"""
每日另類資產新聞彙整 → 推送到 Telegram
架構：Google News RSS 抓新聞 → Python 過濾噪音 → Claude 過濾+語意去重+摘要 → Python 注入(已解析)網址 → Telegram

本版重點改動：
- 【噪音硬刪】NOISE_TITLE_PATTERNS：原告律所樣板稿（shareholder alert / class action / 各家律所名）
  與例行 13F／持倉增減披露，在送進 Claude 之前就用標題樣式刪掉。
- 【撞名硬刪】IRRELEVANT_TITLE_PATTERNS：Brookfield 這個地名/小鎮/房產/學校（伊利諾、威斯康辛、
  Brookfield Center…）與我的持股無關，直接刪掉，避免關鍵字撞名誤判。
- 【跨次語意去重】把過去 DEDUP_DAYS 天「已發過的標題」餵給 Claude 做語意去重。
  → recent_sent_titles limit 提高到 200，避免某一件事（如 Multiplex）的多個變體標題把
    去重窗口吃光，導致前一兩天發過的別件新聞（如 Csquare）漏掉、被重報。
  → sent_state 存 {key: {"d": 日期, "t": 標題}}。
- 【準確性 / 不准腦補敘事】prompt 硬性規定：只寫標題明確出現的事實；摘要長度由標題資訊量決定；
  比喻/並列式標題不准編造因果或對比；摘要內文不准寫來源網域名。
- 【只輸出成品】prompt 明令不准解釋去重/過濾過程；strip_meta_commentary() 為送出前安全網。
- 【觀點來源】OPINION_SOURCES：Seeking Alpha / Motley Fool / Simply Wall St 等分析稿標記
  ［觀點來源］，prompt 要求降級為【觀點】或直接略過。
- 【修死連結】resolve_link()：用 googlenewsdecoder 還原 Google News 加密轉址；失敗退回搜尋連結。

由 GitHub Actions 觸發。環境變數（repo Secrets）：ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
依賴：pip install requests googlenewsdecoder
workflow 需 `permissions: contents: write` + `concurrency:` + 跑完 commit/push sent_state.json。
"""

import os
import re
import sys
import json
import time
import datetime
import email.utils
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from collections import Counter

import requests
from googlenewsdecoder import gnewsdecoder

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

TAIPEI = datetime.timezone(datetime.timedelta(hours=8))
TODAY = datetime.datetime.now(TAIPEI).strftime("%Y-%m-%d")

# ── 去重 / 新鮮度設定 ────────────────────────────────────────
STATE_FILE = Path("sent_state.json")
DEDUP_DAYS = 7
FRESH_HOURS = 48
RESOLVE_LINKS = True          # 是否把 Google 加密轉址還原成真網址（False 則直接用原連結）

# ── 來源黑名單（轉載站 / 內容農場 / 地方小報）────────────────────
BLOCKED_SOURCES = {
    "todayville",
    # MarketBeat 系內容農場（13F 持倉稿的大宗來源；標題樣式也會擋，這裡是雙保險）
    "marketbeat", "etf daily news", "defense world", "tickerreport", "ticker report",
    "modern readers", "american banking news", "dakota financial news",
    "the cerbat gem", "transcript daily", "watch list news", "zolmax",
    # Brookfield 同名小鎮（伊利諾/威斯康辛）的地方/體育小報
    "riverside-brookfield landmark", "maxpreps", "patch", "gmtoday",
    "wisn", "wbal-tv", "dailyvoice",
    # "rebel news",   # 政治立場類要不要擋自己決定
}

# ── 觀點 / 分析來源（不擋，但標記後交給 Claude 降級或略過）──────────
OPINION_SOURCES = {
    "seeking alpha", "motley fool", "simply wall st", "simplywall",
    "kalkine", "gurufocus", "traders union", "marketsmojo", "tipranks",
    "zacks", "investorplace", "wealth awesome", "newsline", "stocktwits",
}

# ── 噪音標題樣式（永遠擋掉，送進 Claude 前就刪）───────────────────
# 原則：只放「幾乎不帶實質資訊」的樣板措辭，避免誤殺真新聞（例如真正的併購入股不會被擋）。
NOISE_TITLE_PATTERNS = {
    # 原告律所「股東警示／集體訴訟召集／investigation」樣板
    "shareholder alert", "investor alert", "class action", "encourages investors",
    "announces investigation", "investigation of", "reminds investors",
    "deadline reminder", "lead plaintiff", "law offices", "investors who lost",
    "investors with losses", "rosen law", "pomerantz", "bronstein",
    "levi & korsinsky", "robbins geller", "glancy prongay", "bragar eagel",
    "kahn swick", "faruqi", "kessler topaz", "schall law", "hagens berman",
    "block & leviton", "the gross law",
    # 例行 13F / 持倉增減披露（aggregator 農場標題格式）
    "13f", "boosts holdings", "boosts stake", "boosts position",
    "lowers holdings", "lowers stake", "lowers position",
    "reduces holdings", "reduces stake", "reduces position",
    "trims holdings", "trims stake", "trims position",
    "raises holdings", "increases holdings", "increases position",
    "cuts holdings", "cuts stake", "lifts holdings", "lifts position",
    "shares sold by", "shares acquired by", "shares purchased by", "shares bought by",
    "sells shares of", "buys shares of", "purchases shares of",
    "million position in", "million stake in", "million holdings in",
    "position lifted", "position raised", "position trimmed", "position boosted",
}

# ── 撞名硬刪（Brookfield 作為地名/小鎮/房產/學校，與持股無關）──────────
IRRELEVANT_TITLE_PATTERNS = {
    "brookfield center", "riverside-brookfield", "town of brookfield",
    "village of brookfield", "brookfield central", "brookfield east",
    "brookfield zoo", "brookfield high", "brookfield fireworks",
    "brookfield dispensary", "brookfield shops", "brookfield, il",
    "brookfield, wi", "brookfield, wisconsin", "brookfield, illinois",
}

# ── 主題黑名單（預設關閉；政治人物個人爭議要不要擋自己決定）─────────
BLOCKED_TITLE_PATTERNS = {
    # "carney", "blind trust", "ethics committee", "conflict of interest",
}

# ── 後設說明關鍵字（送出前安全網，剝掉 Claude 偶爾外洩的去重/過濾說明）──
META_LINE_PATTERNS = (
    "已於近期發送", "已多次發送", "全數略過", "無新增價值", "未重複已發送",
    "近期發送清單", "已發送清單", "重複，全數", "以下為今日",
)

MAX_PER_SOURCE = 6

# ── 發現層設定 ──────────────────────────────────────────────
QUERIES = [
    "Brookfield",
    "Brookfield Corporation",
    "Brookfield Asset Management",
    "Brookfield Infrastructure",
    "Brookfield Renewable",
    "Brookfield real estate",
    "Brookfield receiver distressed",
    "Macquarie Group",
    "Apollo Global Management",
    "KKR",
    "Blackstone",
    "Partners Group",
    "Ares Management",
    "Blue Owl Capital",
    "Oaktree Capital",
    "Bruce Flatt",
    "Howard Marks Oaktree",
    "private credit",
    "infrastructure fund deal",
    "commercial real estate distress",
]

WINDOW = "when:2d"
MAX_ITEMS = 70
GL = "US"


# ── 工具函式 ───────────────────────────────────────────────
def news_key(title: str) -> str:
    """去掉結尾「 - 來源名」後，取正規化前 80 字當去重 key。"""
    t = re.sub(r"\s+[\-\–\—\|]\s+[^\-\–\—\|]+$", "", title or "")
    return " ".join(t.lower().split())[:80]


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:
            return {}
    return {}


def _entry_date(v) -> str:
    return v.get("d", "") if isinstance(v, dict) else (v or "")  # 相容舊格式(純字串日期)


def save_state(state: dict) -> None:
    cutoff = (
        datetime.datetime.now(TAIPEI) - datetime.timedelta(days=DEDUP_DAYS)
    ).strftime("%Y-%m-%d")
    pruned = {k: v for k, v in state.items() if _entry_date(v) >= cutoff}
    STATE_FILE.write_text(json.dumps(pruned, ensure_ascii=False), "utf-8")


def recent_sent_titles(state: dict, days: int = DEDUP_DAYS, limit: int = 200) -> list:
    """近 N 天已發送過的標題（近的在前），給 Claude 做跨次語意去重用。
    limit 拉高到 200：避免單一事件（如 Multiplex）的多個變體標題把窗口吃光，
    害前一兩天發過的別件新聞（如 Csquare）掉出清單而被重報。"""
    cutoff = (
        datetime.datetime.now(TAIPEI) - datetime.timedelta(days=days)
    ).strftime("%Y-%m-%d")
    rows = []
    for v in state.values():
        if isinstance(v, dict) and v.get("t") and v.get("d", "") >= cutoff:
            rows.append((v["d"], v["t"]))
    rows.sort(reverse=True)
    return [t for _, t in rows[:limit]]


def is_fresh(pub: str, hours: int = FRESH_HOURS) -> bool:
    """嚴格兩天制：無法確認在 48h 內就丟（fail-closed）。"""
    if not pub:
        return False
    try:
        dt = email.utils.parsedate_to_datetime(pub)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        age = datetime.datetime.now(datetime.timezone.utc) - dt
        if age < datetime.timedelta(0):
            return False
        return age <= datetime.timedelta(hours=hours)
    except Exception:
        return False


def is_blocked_source(src: str) -> bool:
    s = (src or "").lower()
    return any(b in s for b in BLOCKED_SOURCES)


def is_opinion_source(src: str) -> bool:
    s = (src or "").lower()
    return any(o in s for o in OPINION_SOURCES)


def is_noise_title(title: str) -> bool:
    t = (title or "").lower()
    return any(p in t for p in NOISE_TITLE_PATTERNS)


def is_irrelevant_title(title: str) -> bool:
    t = (title or "").lower()
    return any(p in t for p in IRRELEVANT_TITLE_PATTERNS)


def is_blocked_title(title: str) -> bool:
    if not BLOCKED_TITLE_PATTERNS:
        return False
    t = (title or "").lower()
    return any(p in t for p in BLOCKED_TITLE_PATTERNS)


# ── 發現層 ─────────────────────────────────────────────────
def fetch_rss(query: str) -> list:
    phrase = f'"{query}"' if " " in query else query
    q = urllib.parse.quote(f"{phrase} {WINDOW}")
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl={GL}&ceid={GL}:en"
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        items = []
        for it in root.iter("item"):
            src_el = it.find("source")
            items.append(
                {
                    "title": (it.findtext("title") or "").strip(),
                    "link": (it.findtext("link") or "").strip(),
                    "pubDate": (it.findtext("pubDate") or "").strip(),
                    "source": (src_el.text if src_el is not None else "").strip(),
                    "q": query,
                }
            )
        return items
    except Exception as e:
        print(f"RSS fail [{query}]: {e}", file=sys.stderr)
        return []


def collect(sent_state: dict) -> list:
    """來源/噪音/撞名/主題過濾 → 同次去重 → 跨日(精確)去重 → 48h fail-closed → 單一來源上限。"""
    seen = set()
    per_source = Counter()
    out = []
    for q in QUERIES:
        for it in fetch_rss(q):
            title, src = it["title"], it["source"]
            key = news_key(title)
            if not key or key in seen:
                continue
            # 噪音/黑名單/撞名放在 seen.add 之前：被擋的不佔 key，乾淨版本還有機會被收。
            if (
                is_blocked_source(src)
                or is_noise_title(title)
                or is_irrelevant_title(title)
                or is_blocked_title(title)
            ):
                continue
            seen.add(key)
            if key in sent_state:            # 跨日精確去重
                continue
            if not is_fresh(it["pubDate"]):  # 48h fail-closed
                continue
            if MAX_PER_SOURCE and per_source[src.lower()] >= MAX_PER_SOURCE:
                continue
            per_source[src.lower()] += 1
            it["key"] = key
            out.append(it)
        time.sleep(0.5)
    return out[:MAX_ITEMS]


# ── 推理層（Claude：過濾 + 跨次語意去重 + 摘要）─────────────────────
def build_news_block(items: list) -> str:
    lines = []
    for i, it in enumerate(items, 1):
        tag = " ［觀點來源］" if is_opinion_source(it["source"]) else ""
        lines.append(
            f"[{i}] 來源:{it['source']}{tag} | 時間:{it['pubDate']}\n"
            f"    標題:{it['title']}"
        )
    return "\n".join(lines)


def build_prompt(news_block: str, recent_block: str) -> str:
    return f"""今天是 {TODAY}（台北時間）。下面是我用 Google News RSS 在過去 2 天抓到的另類資產管理業新聞清單（含編號、來源、時間、標題）。

【你只看得到標題，看不到內文——這點決定了下面所有規則】

請你做的事：
1. 只保留「真正重要」的新聞，過濾掉重複、無關、純股價波動、業配與舊聞。
2. 對我的持股（BN／BIPC／BEPC／MQG／APO／KKR）有直接影響的放最前面，標記【持股】。
3. 摘要長度由標題實際資訊量決定：標題只講一件事，就用「一句話」照實複述；不要為了湊到 2-3 句而補上標題沒有的背景、動機、影響或解讀。只有標題本身就含多個事實時才寫到 2-3 句。寧可短，不要腦補。
4. 優先呈報：實體資產出售、商用不動產壞帳/接管、併購與重組進度、評等與展望變動、配息/回購政策、旗艦基金募資與贖回(gate)、管理階層(如 Bruce Flatt、Howard Marks)發言或合作。
5. 沒有重大新聞的板塊直接略過。若清單裡確實沒有重要的，就誠實說「今日無重大新聞」。
6. 結尾給一句「今日板塊情緒：偏多／中性／偏空」並簡述理由。

【不准腦補敘事・最重要】
- 除了標題字面明確寫出的事實，一律不准推論：不准推測公司動機、策略意圖、對股價或估值的影響、與其他公司的比較或分歧、交易背後的原因。
- 標題若是比喻、雙關或語意不明（例如「X reaches for orbit as Y heads for the exit」這種把兩件事並列的標題），只照字面說標題提到了什麼，絕對不要把兩件事連成因果、對比或「同一件事的兩面」，也不要自己編一個解釋。看不懂就只複述字面。
- 標題沒有的金額、估值、股票代號、EPS 數字、人名，一律不准自己編；不確定的實體寧可說「未揭露」。
- 不要自己做單位或貨幣換算：標題寫 crore／lakh／億／美元就照標題原文寫，不要換成其他幣別或單位（例如不要把「INR 3,656 crore」寫成「3,656 億盧比」，也不要自行折算成美元）。印度單位 crore＝千萬、lakh＝十萬，換算極易出錯，一律照原文。

【只輸出成品，不要解釋過程・最重要】
- 只輸出最後選出來要報的新聞。不要解釋你過濾了什麼、跳過了什麼、為什麼跳過、哪些和近期已發送重複。
- 禁止出現這類句子：「以下為今日無新增價值的項目」「此筆已多次發送，全數略過」「（注：…已於近期發送清單中已報…）」「已發送過」「未重複已發送內容」。
- 該略過的就靜默略過，連提都不要提；輸出裡不該有任何關於「去重／過濾／清單」的後設說明。

【避免重複】
- 下面「近期已發送」是過去幾天已報過的標題。今天清單若有一則和它其實是【同一件事】（同一筆交易／財報／報告／訴訟／13F／合作案），即使用字、角度、數字、來源不同，一律不要再報，也不要說明你略過了它。
- 今天清單內部若有多則是同一件事，只挑資訊量最高的一則報，其餘靜默併入或捨棄。
- 特別注意「跟進報導」：一筆大型交易（例如某子公司出售案）成交後，接下來幾天會有大量不同媒體報導同一件事。只要這筆交易你先前已經報過，之後所有跟進報導一律直接不報，不論它是不是我的持股、不論又有幾家媒體跟進。不要「報一則然後加註『無新增事實／跟進報導』」——那還是重複。判斷標準只有一個：核心事件先前報過沒有？報過就丟，連提都不要提。

【觀點來源處理】
- 標記［觀點來源］的是分析/評論稿（Seeking Alpha、Motley Fool、Simply Wall St 等），不是第一手新聞。
- 這類只有在「提供了清單裡其他第一手新聞沒有、且具體的新事實」時才報；否則一律略過。
- 若真要報，標記改用【觀點】（不要用【持股】），並寫明這是第三方分析。

【排除噪音】
- 跳過原告律所的「股東警示／集體訴訟召集／investigation」樣板稿，除非有具體且重大的法律進展（正式起訴、和解金額、法院裁定）。
- 跳過例行 13F／持倉增減披露，除非對「我的持股本身」具策略意義。

【網址處理】
- 不要自己貼網址或寫時間，這些我會用程式補上。
- 摘要內文不要寫出來源網域名（例如 thedeal.com、marketscreener.com），那是給程式用的，寫進內文會被自動連結化弄壞。
- 每則摘要最後放對應標記 [[編號]]，編號就是清單該則開頭 [數字] 的數字。
- 多則併成同一則時，把編號全部相連放結尾，例如 [[1]][[5]][[8]]；不同新聞要分開。

輸出格式：純文字，適合 Telegram。每則之間空一行。開頭寫上日期。整體 2500 字以內。

──── 近期已發送（過去 {DEDUP_DAYS} 天，請務必拿來比對去重）────
{recent_block}

──── 今日新聞清單 ────
{news_block}"""


def summarize(news_block: str, recent_block: str) -> str:
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 4000,
            "messages": [{"role": "user", "content": build_prompt(news_block, recent_block)}],
        },
        timeout=300,
    )
    resp.raise_for_status()
    data = resp.json()
    text = "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    )
    return text.strip() or "（今日沒有產生內容）"


def strip_meta_commentary(text: str) -> str:
    """送出前安全網：即使 prompt 沒擋住，也剝掉去重/過濾的後設說明。
    重點：含 [[編號]] 的行絕不動，以免影響 inject_links 的已發送記錄。"""
    # 1) 去掉 （注：…）/（註：…）裡談到發送/重複/略過/清單的整段括號
    text = re.sub(r"（\s*[注註][：:][^）]*(?:發送|重複|略過|清單)[^）]*）", "", text)
    # 2) 逐行刪掉純後設說明的行（但保留任何含編號標記的行）
    kept = []
    for line in text.splitlines():
        if "[[" in line:
            kept.append(line)
            continue
        if any(p in line for p in META_LINE_PATTERNS):
            continue
        kept.append(line)
    # 3) 壓掉因刪行產生的連續空行
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


# ── 連結還原（修死連結）─────────────────────────────────────
def _gnews_decode(article_url):
    """用維護中的 googlenewsdecoder 還原 Google News 加密轉址。失敗回 None。
    若哪天整批失效，先 `pip install -U googlenewsdecoder` 升級；通常它會跟上 Google 的格式變動。"""
    try:
        res = gnewsdecoder(article_url, interval=1)
        if res.get("status") and str(res.get("decoded_url", "")).startswith("http"):
            return res["decoded_url"]
    except Exception:
        return None
    return None


def resolve_link(google_link: str, title: str) -> str:
    """先試 Google News 解碼 → 再試一般 redirect → 都不行才退回搜尋連結。"""
    if not RESOLVE_LINKS:
        return google_link

    # 1) Google News 加密連結：用 googlenewsdecoder 解碼
    if "news.google.com" in google_link:
        real = _gnews_decode(google_link)
        if real:
            return real

    # 2) 一般轉址（非 Google News，或解碼失敗時再試一次跟轉址）
    try:
        r = requests.get(
            google_link, timeout=10, allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        final = r.url or ""
        if final.startswith("http") and "news.google.com" not in final and "google.com/sorry" not in final:
            return final
    except Exception:
        pass

    # 3) 一定打得開的退路
    return "https://news.google.com/search?q=" + urllib.parse.quote(title or google_link)


def inject_links(digest: str, items: list):
    """把 [[編號]] 換成(還原後的)網址。同段多個來源只顯示第一個，但全部記為已發送。"""
    idx = {str(i): it for i, it in enumerate(items, 1)}
    sent_keys = set()
    run = re.compile(r"\[\[\d+\]\](?:\s*\[\[\d+\]\])*")

    def repl(m):
        nums = re.findall(r"\[\[(\d+)\]\]", m.group(0))
        shown = ""
        for n in nums:
            it = idx.get(n)
            if not it:
                continue
            sent_keys.add(it["key"])
            if not shown:
                url = resolve_link(it["link"], it["title"])
                shown = f"\n{url}\n🕐 {it['pubDate']}"
        return shown

    text = run.sub(repl, digest)
    return text, sent_keys


# ── 送出 ───────────────────────────────────────────────────
def send_telegram(text: str) -> None:
    LIMIT = 4000
    blocks = text.split("\n\n")
    chunks, cur = [], ""
    for b in blocks:
        piece = (cur + "\n\n" + b) if cur else b
        if len(piece) <= LIMIT:
            cur = piece
        else:
            if cur:
                chunks.append(cur)
            if len(b) <= LIMIT:
                cur = b
            else:
                for j in range(0, len(b), LIMIT):
                    chunks.append(b[j : j + LIMIT])
                cur = ""
    if cur:
        chunks.append(cur)

    for chunk in chunks:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "disable_web_page_preview": True},
            timeout=60,
        )
        r.raise_for_status()


if __name__ == "__main__":
    try:
        sent_state = load_state()
        items = collect(sent_state)
        print(f"抓到 {len(items)} 則（噪音/來源/撞名過濾 + 精確去重 + 48h 後）", file=sys.stderr)

        if not items:
            send_telegram(f"📅 {TODAY}\n今日近 48 小時內沒有新的（未發送過的）新聞。")
            save_state(sent_state)
            sys.exit(0)

        recent_block = "\n".join(f"- {t}" for t in recent_sent_titles(sent_state)) or "（無）"
        digest = summarize(build_news_block(items), recent_block)
        digest = strip_meta_commentary(digest)          # 送出前剝掉殘留的後設說明
        final_text, sent_keys = inject_links(digest, items)
        send_telegram(final_text)

        # 記下這次「實際發出去」的標題（含標題本身），下次才能做跨次語意去重
        title_by_key = {it["key"]: it["title"] for it in items}
        for k in sent_keys:
            sent_state[k] = {"d": TODAY, "t": title_by_key.get(k, "")}
        save_state(sent_state)

        print(f"Sent OK；本次標記 {len(sent_keys)} 則為已發送", file=sys.stderr)
    except Exception as e:
        try:
            send_telegram(f"⚠️ 今日新聞彙整失敗：{e}")
        except Exception:
            pass
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)   # 讓 GitHub Actions 把這次 run 標記成失敗
