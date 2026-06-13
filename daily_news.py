"""
每日另類資產新聞彙整 → 推送到 Telegram
由 GitHub Actions 在雲端定時觸發，電腦不用開機。
需要的環境變數（存在 GitHub repo Secrets）：
  ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import os
import sys
import datetime
import requests

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# 台北時間日期
TAIPEI = datetime.timezone(datetime.timedelta(hours=8))
TODAY = datetime.datetime.now(TAIPEI).strftime("%Y-%m-%d")

PROMPT = f"""今天是 {TODAY}（台北時間）。請幫我彙整過去 24 小時內、另類資產管理業的重點新聞。

範圍：Brookfield 生態系（包含母公司 Brookfield Corporation 及子公司 BN、BAM、BIPC、BEPC、BIP、BEP、BNRE 等）、Macquarie（MQG）、Apollo（APO）、KKR、
Blackstone(BX)、Partners Group(PHGN)、Ares(ARES)、EQT AB(EQT)、CVC capital partners(CVC)、CARLYLE GROUP(CG)、Blue Owl Capital Inc.(OWL)；
以及影響這些板塊的總經因素（利率、私募信貸、基礎設施、再生能源政策、商用不動產）。
特別留意：上述四大巨頭在過去 24 小時內的最新實體資產交易（特別是商用不動產、房地產大宗收購、基礎設施項目進度）、併購與重組案、評等變動、配息政策、旗艦基金募資與贖回動態。
特別留意：
- 過去 24 小時內上述公司任何「實體資產出售、購物中心或商用不動產壞帳/接管（Receiver/Distressed asset）」的新聞。
- 管理階層（如 Bruce Flatt、Howard Marks 等）的最新對話、專訪或合作動態。
要求：
- 如果遇到 Bloomberg、Barron's、The Real Deal、Reuters 等媒體的付費牆（Paywall）或標題，請不要直接放棄；
- 務必從搜尋結果的摘要（Snippets）中提煉出關鍵交易數據（例如：邁阿密地產出售金額、新澤西購物中心估值變動、Oaktree 與 Brookfield 的合作進度），並呈報上來。

要求：
- 只收過去 24 小時的新聞，每則附上來源連結與發布時間。
- 每則用 2-3 句繁體中文摘要，用自己的話寫，不要照抄原文。
- 對我的持股（BN／BIPC／BEPC／MQG／APO／KKR）有直接影響的項目放最前面，並標記【持股】。
- 特別留意：併購與重組案進度、評等與展望變動、配息或回購政策、管理階層發言、
  旗艦基金募資與贖回（gate）動態。
- 沒有重大新聞的板塊直接略過，不要硬湊；若整天都沒有重要新聞，就誠實說「今日無重大新聞」。
- 結尾給一句「今日板塊情緒：偏多／中性／偏空」並簡述理由。

輸出格式：純文字，適合在 Telegram 閱讀。用短段落，每則新聞後面直接放原始網址
（不要用 Markdown 連結語法）。開頭寫上日期。整體長度盡量控制在 2500 字以內。"""


def get_digest() -> str:
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
            "messages": [{"role": "user", "content": PROMPT}],
            "tools": [
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": 6,  # 控制成本：最多搜尋 6 次
                }
            ],
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
    # Telegram 單則上限 4096 字，超過就分段送
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
        digest = get_digest()
        send_telegram(digest)
        print("Sent OK")
    except Exception as e:
        # 失敗時也推一則到 Telegram，讓你知道今天沒跑成功
        try:
            send_telegram(f"⚠️ 今日新聞彙整失敗：{e}")
        except Exception:
            pass
        print("Error:", e, file=sys.stderr)
        sys.exit(1)
