"""
每日另類資產新聞彙整 → 推送到 Telegram
架構：Google News RSS 抓新聞 → Claude 過濾+摘要 → Python 注入網址 → Telegram

本版相對上一版的改動（共 3 處）：
- 【召回率】QUERIES 新增廣撈關鍵字 "Brookfield"，補抓標題措辭鬆散
  （例：「Renewable Energy Deal with Brookfield」）的交易新聞。
- 【顯示】inject_links 改成：同一則新聞若併了多個來源，只顯示第一個網址，
  但其餘來源照樣記為「已發送」，畫面清爽且跨日去重仍完整。
- 【提示】prompt 配合說明：同一則新聞可把多個來源編號連在一起放。

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

import requests

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

TAIPEI = datetime.timezone(datetime.timedelta(hours=8))
TODAY = datetime.datetime.now(TAIPEI).strftime("%Y-%m-%d")

# ── 去重 / 新鮮度設定 ────────────────────────────────────────
STATE_FILE = Path("sent_state.json")  # 跨日去重狀態（會被 Actions commit 回 repo）
DEDUP_DAYS = 7                        # 已發送紀錄保留天數，超過就清掉
FRESH_HOURS = 48                      # 只發布近 48 小時內的新聞（想抓更舊就改 72，WINDOW 也要一起改）

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
    """用標題正規化後的前 80 字當去重 key（不同 feed 會撈到同一則）。"""
    return " ".join(title.lower().split())[:80]


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
    if not pub:
        return True
    try:
        dt = email.utils.parsedate_to_datetime(pub)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        age = datetime.datetime.now(datetime.timezone.utc) - dt
        return age <= datetime.timedelta(hours=hours)
    except Exception:
        return True


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
    """跑完所有關鍵字，套用：同次去重 + 跨日去重 + 48 小時過濾。"""
    seen = set()
    out = []
    for q in QUERIES:
        for it in fetch_rss(q):
            key = news_key(it["title"])
            if not key or key in seen:
                continue
            seen.add(key)
            if key in sent_state:            # 跨日去重：之前已發送過就跳過
                continue
            if not is_fresh(it["pubDate"]):  # 硬性 48 小時
                continue
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
7. 結尾給一句「今日板塊情緒：偏多／中性／偏空」並簡述理由。

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
        print(f"抓到 {len(items)} 則（跨日去重 + 48h 過濾後）", file=sys.stderr)

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
