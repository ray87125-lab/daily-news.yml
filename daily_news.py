"""
每日另類資產新聞彙整 → 推送到 Telegram
架構：Google News RSS 抓新聞 → Python 過濾噪音 → Claude 過濾+語意去重+摘要 → Python 注入(已解析)網址 → Telegram

本版重點改動：
- 【噪音硬刪】NOISE_TITLE_PATTERNS：原告律所樣板稿（shareholder alert / class action / 各家律所名）
  與例行 13F／持倉增減披露（trims holdings、shares sold by、$X million position…），
  在送進 Claude 之前就用標題樣式刪掉，Claude 根本看不到，保證不出現。
- 【跨次語意去重】把過去 DEDUP_DAYS 天「已發過的標題」餵給 Claude，要求它把
  「同一件事、不同文章/來源/用字」的新聞一律不再報。純比對標題抓不到，只能靠語意。
  → sent_state 改存 {key: {"d": 日期, "t": 標題}}，才有歷史標題可餵。
- 【準確性】prompt 硬性規定：只能寫標題明確出現的事實與數字，沒有的代號/金額/EPS 不准杜撰
  （直接堵掉先前「APO 子公司 APOS」這種捏造）。
- 【修死連結】resolve_link()：把 Google News 的加密轉址盡量還原成真正文章網址；
  還原不了就退回一個一定打得開的 Google News 搜尋連結，不再出現點了打不開的連結。

由 GitHub Actions 觸發。環境變數（repo Secrets）：ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
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

# ── 來源黑名單（轉載站 / 內容農場）─────────────────────────────
BLOCKED_SOURCES = {
    "todayville",
    # MarketBeat 系內容農場（13F 持倉稿的大宗來源；標題樣式也會擋，這裡是雙保險）
    "marketbeat", "etf daily news", "defense world", "tickerreport", "ticker report",
    "modern readers", "american banking news", "dakota financial news",
    "the cerbat gem", "transcript daily", "watch list news", "zolmax",
    # "rebel news",   # 政治立場類要不要擋自己決定
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

# ── 主題黑名單（預設關閉；政治人物個人爭議要不要擋自己決定）─────────
BLOCKED_TITLE_PATTERNS = {
    # "carney", "blind trust", "ethics committee", "conflict of interest",
}

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


def recent_sent_titles(state: dict, days: int = DEDUP_DAYS, limit: int = 50) -> list:
    """近 N 天已發送過的標題（近的在前），給 Claude 做跨次語意去重用。"""
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


def is_noise_title(title: str) -> bool:
    t = (title or "").lower()
    return any(p in t for p in NOISE_TITLE_PATTERNS)


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
    """來源/噪音/主題過濾 → 同次去重 → 跨日(精確)去重 → 48h fail-closed → 單一來源上限。"""
    seen = set()
    per_source = Counter()
    out = []
    for q in QUERIES:
        for it in fetch_rss(q):
            title, src = it["title"], it["source"]
            key = news_key(title)
            if not key or key in seen:
                continue
            # 噪音/黑名單放在 seen.add 之前：被擋的不佔 key，乾淨版本還有機會被收。
            if is_blocked_source(src) or is_noise_title(title) or is_blocked_title(title):
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
        lines.append(
            f"[{i}] 來源:{it['source']} | 時間:{it['pubDate']}\n"
            f"    標題:{it['title']}"
        )
    return "\n".join(lines)


def build_prompt(news_block: str, recent_block: str) -> str:
    return f"""今天是 {TODAY}（台北時間）。下面是我用 Google News RSS 在過去 2 天抓到的另類資產管理業新聞清單（含編號、來源、時間、標題）。

請你做的事：
1. 只保留「真正重要」的新聞，過濾掉重複、無關、純股價波動、業配與舊聞。
2. 對我的持股（BN／BIPC／BEPC／MQG／APO／KKR）有直接影響的放最前面，標記【持股】。
3. 每則用 2-3 句繁體中文摘要，用自己的話寫，不要照抄原文。
4. 優先呈報：實體資產出售、商用不動產壞帳/接管、併購與重組進度、評等與展望變動、
   配息/回購政策、旗艦基金募資與贖回(gate)、管理階層(如 Bruce Flatt、Howard Marks)發言或合作。
5. 沒有重大新聞的板塊直接略過。若清單裡確實沒有重要的，就誠實說「今日無重大新聞」。
6. 結尾給一句「今日板塊情緒：偏多／中性／偏空」並簡述理由。

【避免重複・最重要】
- 下面「近期已發送」是過去幾天已經報過的新聞標題。今天清單中若有任何一則，
  和「近期已發送」其實是【同一件事】（同一筆交易／同一份財報／同一份報告／同一樁訴訟／
  同一份 13F／同一個合作案），即使用字、角度、數字、來源不同，**一律不要再報**。
- 今天清單內部若有多則其實是同一件事，**只挑資訊量最高的一則**報，其餘併入或捨棄。

【排除噪音・最重要】
- 跳過原告律所的「股東警示／集體訴訟召集／investigation」樣板稿，除非有具體且重大的法律
  進展（正式起訴、和解金額、法院裁定）。
- 跳過例行 13F／持倉增減披露，除非是對「我的持股本身」具策略意義的重大變動。

【準確性・最重要】
- 只寫標題明確出現的事實與數字；標題沒有的金額、估值、股票代號、EPS 數字，**一律不要自己編**。
- 不確定的實體名稱或代號不要硬寫，寧可說「未揭露」也不要杜撰。

【網址處理】
- 不要自己貼網址或寫時間，這些我會用程式補上。
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


# ── 連結還原（修死連結）─────────────────────────────────────
def resolve_link(google_link: str, title: str) -> str:
    """Google News 的 articles/CBMi... 是加密轉址，常常打不開。
    先嘗試跟著轉址拿到真正文章網址；拿不到就退回一個一定打得開的 Google News 搜尋連結。"""
    if not RESOLVE_LINKS:
        return google_link
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
        print(f"抓到 {len(items)} 則（噪音/來源過濾 + 精確去重 + 48h 後）", file=sys.stderr)

        if not items:
            send_telegram(f"📅 {TODAY}\n今日近 48 小時內沒有新的（未發送過的）新聞。")
            save_state(sent_state)
            sys.exit(0)

        recent_block = "\n".join(f"- {t}" for t in recent_sent_titles(sent_state)) or "（無）"
        digest = summarize(build_news_block(items), recent_block)
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
        print("Error:", e, file=sys.stderr)
        sys.exit(1)
