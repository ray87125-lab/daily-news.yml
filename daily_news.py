"""
每日另類資產新聞彙整 → 推送到 Telegram
架構：Google News RSS 抓新聞 → Claude 過濾+摘要 → Python 注入網址 → Telegram

本版相對上一版的改動（共 6 處，重點都在「擋掉轉載舊聞」）：
- 【新鮮度・改 fail-closed】is_fresh 由「無法判斷就放行」改成「無法判斷就丟」。
  沒有 pubDate、解析失敗、或時間在未來，一律當成不新鮮 → 丟掉。
  （嚴格兩天制：寧可錯殺無日期的，不要放行舊聞。）
- 【來源黑名單】新增 BLOCKED_SOURCES：用 RSS 的 <source> 名稱比對，
  擋掉轉載站／內容農場。上次回鍋 Carney 舊聞的 todayville 已預設放進去。
  （這是真正能擋住「轉載舊文拿到新時間戳」的那一層。）
- 【主題黑名單・預設關閉】新增 BLOCKED_TITLE_PATTERNS：標題含特定字就丟。
  預設為空，附上範例，你要不要擋政治人物個人爭議自己決定（見下方說明）。
- 【去重 key 修正】news_key 先去掉 Google News 慣性加在結尾的「 - 來源名」，
  讓同一則新聞在不同來源也能對到同一個 key，跨來源去重更準。
- 【單一來源上限】新增 MAX_PER_SOURCE：避免單一媒體洗版。
- 【提示強化】prompt 多一條：提醒清單上的「時間」可能是轉載時間戳，
  若標題明顯在講「已發生一段時間的事件」要保守當舊聞略過。

由 GitHub Actions 在雲端定時觸發。需要的環境變數（GitHub repo Secrets）：
  ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
另：workflow 需有 `permissions: contents: write` 才能把 sent_state.json commit 回 repo。
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
STATE_FILE = Path("sent_state.json")  # 跨日去重狀態（會被 Actions commit 回 repo）
DEDUP_DAYS = 7                        # 已發送紀錄保留天數，超過就清掉
FRESH_HOURS = 48                      # 只發布近 48 小時內的新聞（兩天制；改 72 時 WINDOW 也要一起改）

# ── 來源 / 主題過濾（新增）──────────────────────────────────
# 已知的轉載站 / 內容農場 / 低訊號來源：用 RSS 的 <source> 名稱比對（不分大小寫、部分比對即可）。
# 這一層才是真正能擋住「舊文被轉載後拿到新時間戳」的防線——時間過濾擋不了它。
BLOCKED_SOURCES = {
    "todayville",        # ← 上次把 4 月的 Carney 道德委員會舊聞回鍋的轉載站
    # "rebel news",      # 想擋再自己加；維持中立，要不要擋由你決定
    # "probe international",
}

# 標題含這些字（不分大小寫、部分比對）就丟掉。
# 預設「空集合」= 不啟用，因為這類新聞對 BN 持股人其實可能是監管/政治風險訊號。
# 如果你確定不想再收到「卡尼個人爭議」這類非公司營運新聞，就把下面幾行的註解拿掉。
BLOCKED_TITLE_PATTERNS = {
    # "carney",
    # "blind trust",
    # "ethics committee",
    # "conflict of interest",
}

MAX_PER_SOURCE = 6   # 單一來源（媒體）每次最多收幾則，避免洗版；設 0 或 None 取消限制

# ── 發現層設定 ──────────────────────────────────────────────
QUERIES = [
    # Brookfield 生態系
    "Brookfield",                      # ★廣撈：標題有 Brookfield 就抓，補捉鬆散措辭的交易
    "Brookfield Corporation",
    "Brookfield Asset Management",
    "Brookfield Infrastructure",
    "Brookfield Renewable",
    "Brookfield real estate",
    "Brookfield receiver distressed",
    # 同業
    "Macquarie Group",
    "Apollo Global Management",
    "KKR",
    "Blackstone",
    "Partners Group",
    "Ares Management",
    "Blue Owl Capital",
    "Oaktree Capital",
    # 管理階層 & 總經
    "Bruce Flatt",
    "Howard Marks Oaktree",
    "private credit",
    "infrastructure fund deal",
    "commercial real estate distress",
]

WINDOW = "when:2d"   # 想放寬成 72 小時就改 "when:3d"（並把上面 FRESH_HOURS 改 72）
MAX_ITEMS = 70
GL = "US"


# ── 工具函式：去重 key / 狀態存取 / 新鮮度 ──────────────────────
def news_key(title: str) -> str:
    """用標題正規化後的前 80 字當去重 key（不同 feed 會撈到同一則）。
    先去掉 Google News 慣性加在結尾的「 - 來源名 / | 來源名」，
    讓同一則新聞在不同來源也能對到同一個 key，跨來源去重才準。"""
    t = re.sub(r"\s+[\-\–\—\|]\s+[^\-\–\—\|]+$", "", title or "")
    return " ".join(t.lower().split())[:80]


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    cutoff = (
        datetime.datetime.now(TAIPEI) - datetime.timedelta(days=DEDUP_DAYS)
    ).strftime("%Y-%m-%d")
    pruned = {k: v for k, v in state.items() if v >= cutoff}
    STATE_FILE.write_text(json.dumps(pruned, ensure_ascii=False), "utf-8")


def is_fresh(pub: str, hours: int = FRESH_HOURS) -> bool:
    """嚴格兩天制：無法確認是 48 小時內的，一律當成不新鮮（fail-closed）。
    這樣沒有 pubDate、解析失敗、或時間異常（未來）的項目都會被擋下。"""
    if not pub:
        return False                       # 沒有時間 = 無法驗證 → 丟
    try:
        dt = email.utils.parsedate_to_datetime(pub)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        age = now - dt
        if age < datetime.timedelta(0):    # 時間在未來 = 異常 → 丟
            return False
        return age <= datetime.timedelta(hours=hours)
    except Exception:
        return False                       # 解析失敗 = 無法驗證 → 丟


def is_blocked_source(src: str) -> bool:
    s = (src or "").lower()
    return any(b in s for b in BLOCKED_SOURCES)


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
    """跑完所有關鍵字，套用：來源/主題黑名單 + 同次去重 + 跨日去重 + 48 小時過濾 + 單一來源上限。"""
    seen = set()
    per_source = Counter()
    out = []
    for q in QUERIES:
        for it in fetch_rss(q):
            title, src = it["title"], it["source"]
            key = news_key(title)
            if not key or key in seen:
                continue
            # 黑名單放在 seen.add 之前：被擋掉的版本不佔用 key，
            # 讓同一則新聞的「乾淨來源版本」之後還有機會被收錄。
            if is_blocked_source(src) or is_blocked_title(title):
                continue
            seen.add(key)
            if key in sent_state:            # 跨日去重：之前已發送過就跳過
                continue
            if not is_fresh(it["pubDate"]):  # 硬性 48 小時（fail-closed）
                continue
            if MAX_PER_SOURCE and per_source[src.lower()] >= MAX_PER_SOURCE:
                continue                     # 單一來源洗版上限
            per_source[src.lower()] += 1
            it["key"] = key
            out.append(it)
        time.sleep(0.5)
    return out[:MAX_ITEMS]


# ── 推理層（Claude 摘要，不抄網址）──────────────────────────────
def build_news_block(items: list) -> str:
    lines = []
    for i, it in enumerate(items, 1):
        lines.append(
            f"[{i}] 來源:{it['source']} | 時間:{it['pubDate']}\n"
            f"    標題:{it['title']}"
        )
    return "\n".join(lines)


def build_prompt(news_block: str) -> str:
    return f"""今天是 {TODAY}（台北時間）。下面是我用 Google News RSS 在過去 2 天抓到的另類資產管理業新聞清單（含編號、來源、發布時間、標題）。

請你做的事：
1. 只保留「真正重要」的新聞，過濾掉重複、無關、純股價波動、業配與明顯舊聞。
2. 對我的持股（BN／BIPC／BEPC／MQG／APO／KKR）有直接影響的放最前面，並標記【持股】。
3. 每則用 2-3 句繁體中文摘要，用自己的話寫，不要照抄原文。
4. 盡量從標題中提煉關鍵交易數據（金額、估值、進度、評等）。
5. 特別優先呈報：實體資產出售、商用不動產壞帳/接管（receiver / distressed asset）、
   併購與重組案進度、評等與展望變動、配息/回購政策、旗艦基金募資與贖回（gate）動態、
   管理階層（如 Bruce Flatt、Howard Marks）發言或合作。
6. 沒有重大新聞的板塊直接略過，不要硬湊。若清單裡確實沒有重要的，就誠實說「今日無重大新聞」。
7. 【重要・防回鍋舊聞】清單上的「時間」是 Google News 的索引/轉載時間，
   有可能是「舊聞被轉載後拿到的新時間戳」。若標題明顯在講一件「已經發生一段時間的事」
   （例如某委員會報告、某訴訟判決、某人就任、某已定案的政策），而且讀起來不像是
   「最新進度更新」，請保守地當成舊聞略過或大幅降權，不要當成今日新聞推給我。
8. 結尾給一句「今日板塊情緒：偏多／中性／偏空」並簡述理由。

【重要】網址處理：
- 不要自己貼任何網址，也不要寫發布時間，這些我會用程式自動補上。
- 在每則摘要的最後放對應標記 [[編號]]，編號就是上面清單該則開頭 [數字] 的數字。
- 如果你把好幾條清單併成「同一則新聞」，就把它們的編號全部「連在一起」放在那段結尾，
  例如 [[1]][[5]][[8]]。我只會顯示第一個網址，但會把全部來源記錄起來，避免之後重複推送。
- 不同的新聞要分開，不要亂標。

輸出格式：純文字，適合在 Telegram 閱讀。每則之間空一行（一個空白行）。
開頭寫上日期。整體長度盡量控制在 2500 字以內。

──── 新聞清單 ────
{news_block}"""


def summarize(news_block: str) -> str:
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
            "messages": [{"role": "user", "content": build_prompt(news_block)}],
        },
        timeout=300,
    )
    resp.raise_for_status()
    data = resp.json()
    text = "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    )
    return text.strip() or "（今日沒有產生內容）"


def inject_links(digest: str, items: list):
    """把 [[編號]] 換成網址。同一段若連續標了多個來源（同一則新聞的多個出處），
    只顯示第一個網址，但全部都記為『已發送』，畫面清爽且跨日去重完整。"""
    idx = {str(i): it for i, it in enumerate(items, 1)}
    sent_keys = set()

    run = re.compile(r"\[\[\d+\]\](?:\s*\[\[\d+\]\])*")  # 一串相鄰的標記

    def repl(m):
        nums = re.findall(r"\[\[(\d+)\]\]", m.group(0))
        shown = ""
        for n in nums:
            it = idx.get(n)
            if not it:
                continue
            sent_keys.add(it["key"])      # 全部記為已發送（去重用）
            if not shown:                  # 只顯示第一個網址
                shown = f"\n{it['link']}\n🕐 {it['pubDate']}"
        return shown

    text = run.sub(repl, digest)
    return text, sent_keys


# ── 送出（依段落為界切塊，網址不會被切斷）────────────────────────
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
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "disable_web_page_preview": True,
            },
            timeout=60,
        )
        r.raise_for_status()


if __name__ == "__main__":
    try:
        sent_state = load_state()
        items = collect(sent_state)
        print(f"抓到 {len(items)} 則（來源/主題過濾 + 跨日去重 + 48h fail-closed 後）", file=sys.stderr)

        if not items:
            send_telegram(f"📅 {TODAY}\n今日近 48 小時內沒有新的（未發送過的）新聞。")
            save_state(sent_state)
            sys.exit(0)

        digest = summarize(build_news_block(items))
        final_text, sent_keys = inject_links(digest, items)
        send_telegram(final_text)

        for k in sent_keys:
            sent_state[k] = TODAY
        save_state(sent_state)

        print(f"Sent OK；本次標記 {len(sent_keys)} 則為已發送", file=sys.stderr)
    except Exception as e:
        try:
            send_telegram(f"⚠️ 今日新聞彙整失敗：{e}")
        except Exception:
            pass
        print("Error:", e, file=sys.stderr)
        sys.exit(1)
