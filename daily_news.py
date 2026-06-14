"""
每日另類資產新聞彙整 → 推送到 Telegram
架構：Google News RSS 抓新聞（保證新鮮、全面）→ Claude 過濾+摘要 → Telegram

為什麼這樣改：
- 舊版把「發現新聞」和「摘要」綁在同一次 web_search 呼叫，且只給 6 次搜尋，
  導致突發/付費牆新聞（Bloomberg、Barron's、The Real Deal）當天排不上去而被漏掉。
- 新版改用 Google News RSS 當「發現層」：每個關鍵字各抓一條、限定近 48 小時，
  把全面的標題清單交給 Claude 做「推理層」（去重、過濾、摘要、標持股、情緒判斷）。

由 GitHub Actions 在雲端定時觸發，電腦不用開機。
需要的環境變數（存在 GitHub repo Secrets）：
  ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import os
import sys
import time
import datetime
import urllib.parse
import xml.etree.ElementTree as ET

import requests

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

TAIPEI = datetime.timezone(datetime.timedelta(hours=8))
TODAY = datetime.datetime.now(TAIPEI).strftime("%Y-%m-%d")

# ── 發現層設定 ──────────────────────────────────────────────
# 每個關鍵字會獨立抓一條 RSS。這就是「全面」的關鍵：與其下一個大範圍查詢，
# 不如每個實體各搜一次，這樣突發新聞才不會被熱門常青頁面蓋過去。
QUERIES = [
    # Brookfield 生態系（拆細，避免互相蓋掉）
    "Brookfield Corporation",
    "Brookfield Asset Management",
    "Brookfield Infrastructure",
    "Brookfield Renewable",
    "Brookfield real estate",          # 實體資產交易常掛在這裡
    "Brookfield receiver distressed",  # 壞帳/接管/不良資產
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

WINDOW = "when:2d"   # 只抓近 2 天（可改 when:1d 更嚴、when:3d 更寬）
MAX_ITEMS = 70       # 丟給 Claude 的新聞上限，控制 token 成本
GL = "US"            # 地區；Macquarie 想加強可另跑一輪 gl=AU


def fetch_rss(query: str) -> list:
    """抓單一關鍵字的 Google News RSS，回傳新聞 item 清單。"""
    phrase = f'"{query}"' if " " in query else query
    q = urllib.parse.quote(f"{phrase} {WINDOW}")
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl={GL}&ceid={GL}:en"
    try:
        r = requests.get(
            url, timeout=30, headers={"User-Agent": "Mozilla/5.0"}
        )
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
    except Exception as e:  # 單一 feed 失敗不影響其他
        print(f"RSS fail [{query}]: {e}", file=sys.stderr)
        return []


def collect() -> list:
    """跑完所有關鍵字、去重後回傳合併清單。"""
    seen = set()
    out = []
    for q in QUERIES:
        for it in fetch_rss(q):
            # 用標題前 80 字當去重 key（不同 feed 會撈到同一則）
            key = it["title"][:80].lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(it)
        time.sleep(0.5)  # 對 Google 客氣一點，避免被擋
    return out[:MAX_ITEMS]


def build_news_block(items: list) -> str:
    lines = []
    for i, it in enumerate(items, 1):
        lines.append(
            f"{i}. [{it['source']}] {it['title']}\n"
            f"   時間:{it['pubDate']}\n"
            f"   連結:{it['link']}"
        )
    return "\n".join(lines)


def build_prompt(news_block: str) -> str:
    return f"""今天是 {TODAY}（台北時間）。下面是我用 Google News RSS 在過去 2 天抓到的另類資產管理業新聞清單（已含標題、來源、發布時間、連結）。

請你做的事：
1. 只保留「真正重要」的新聞，過濾掉重複、無關、純股價波動、業配與明顯舊聞。
2. 對我的持股（BN／BIPC／BEPC／MQG／APO／KKR）有直接影響的放最前面，並標記【持股】。
3. 每則用 2-3 句繁體中文摘要，用自己的話寫，不要照抄原文。每則後面附上原始連結與發布時間。
4. 盡量從標題中提煉關鍵交易數據（金額、估值、進度、評等）。
5. 特別優先呈報：實體資產出售、商用不動產壞帳/接管（receiver / distressed asset）、
   併購與重組案進度、評等與展望變動、配息/回購政策、旗艦基金募資與贖回（gate）動態、
   管理階層（如 Bruce Flatt、Howard Marks）發言或合作。
6. 沒有重大新聞的板塊直接略過，不要硬湊。若清單裡確實沒有重要的，就誠實說「今日無重大新聞」。
7. 結尾給一句「今日板塊情緒：偏多／中性／偏空」並簡述理由。

輸出格式：純文字，適合在 Telegram 閱讀。用短段落，每則新聞後面直接放原始網址
（不要用 Markdown 連結語法）。開頭寫上日期。整體長度盡量控制在 2500 字以內。

──── 新聞清單 ────
{news_block}"""


def summarize(news_block: str) -> str:
    """把 RSS 清單交給 Claude 過濾 + 摘要（不需要它自己再上網搜）。"""
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 3000,
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


def send_telegram(text: str) -> None:
    for i in range(0, len(text), 4000):
        chunk = text[i : i + 4000]
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
        items = collect()
        print(f"抓到 {len(items)} 則（去重後）", file=sys.stderr)
        if not items:
            send_telegram(f"📅 {TODAY}\n今日 RSS 沒有抓到任何新聞，請檢查關鍵字或網路。")
            sys.exit(0)
        digest = summarize(build_news_block(items))
        send_telegram(digest)
        print("Sent OK")
    except Exception as e:
        try:
            send_telegram(f"⚠️ 今日新聞彙整失敗：{e}")
        except Exception:
            pass
        print("Error:", e, file=sys.stderr)
        sys.exit(1)
