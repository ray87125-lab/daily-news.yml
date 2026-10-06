"""
每日另類資產新聞彙整 → 推送到 Telegram
架構：Google News RSS 抓新聞（分群組＋配額）→ Python 過濾噪音 → Claude 過濾+語意去重+摘要
      → Python 數字查核 → Python 注入(已解析)網址 → Telegram

本版重點改動：
- 【噪音硬刪】NOISE_TITLE_PATTERNS：原告律所樣板稿（shareholder alert / class action / 各家律所名）
  與例行 13F／持倉增減披露，在送進 Claude 之前就用標題樣式刪掉。
  ★本次新增：純股價波動 / 技術線型 / 每日漲跌 recap（stock price forecast、outperforms market、
    % this week…）也在這層硬刪——prompt 雖已要求略過，但「Up 3% This Week」仍會漏進來，做雙保險。
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
  ★本次新增：kavout（演算法軟文）、ad hoc news（內容回收站）也納入降級。
- 【修死連結】resolve_link()：用 googlenewsdecoder 還原 Google News 加密轉址；失敗退回搜尋連結。

2026-09-28 修正（依 9/24、9/26、9/27 三天的實際輸出回推）：
- ★【連結全數變搜尋頁】googlenewsdecoder 0.2.x 把回傳鍵從 "status" 改成 "success"，
  舊程式只認 "status"，於是每一則都判定解碼失敗、默默退回 news.google.com/search。
  → _gnews_decode() 兩種鍵都認，失敗時把原因印到 log；workflow 請把版本釘住。
- ★【持股漏標】Brookfield Renewable（Avaada）、Oaktree（Utmost）沒被標【持股】。
  → prompt 加入 HOLDING_ENTITY_MAP：子公司／旗下平台 → 對應持股代號。
- ★【舊聞當新聞】NAVER×Brookfield（7 月事件）9/24 又被當成新利多；7 天去重窗口太短。
  → 標題記憶拉長到 TITLE_MEMORY_DAYS=90 天；超過 7 天的同一事件只有出現新的
    具體事實（簽約／交割／金額變動）才報，並標【進度更新】、不計入情緒。
- ★【情緒來源不明】9/26 情緒寫「BN、BEPC 觸及 52 週低點」，但報出的新聞沒有一則提到。
  → 52-week 高低點稿在 Python 層硬刪；prompt 規定情緒只能根據「本次有報出」的新聞，
    並依確定性（已完成 > 排他談判 > 非約束性 > 會面/計劃）與對持股的直接程度加權，
    觀點稿、管理人自家研究、進度更新不計入。
- 【管理人自家研究】Apollo 等自己發布的市場觀點當成公司事件報出 → prompt 規定降級為【觀點】或略過。
- 【進度更新收緊】（9/28 試跑後補）Kalkine 的分析稿「deal agreed but the go-shop is open」被當成
  RWC 進度更新報出，但 RWC 早在 9/16 就簽了 SID，不是新事實；還外洩了「（核心事件先前已報…）」。
  → 進度更新必須：非觀點來源、標題是剛發生的動作、事實沒報過；strip_meta_commentary 另外剝掉這類括號。
- 【編號標記容錯】Claude 偶爾寫成 [[1]], [[5]] 或 [[1]]、[[5]]，舊 regex 只吃空白，
  會在內文中間插出第二條網址 → inject_links 允許逗號／頓號分隔。

2026-10-07 修正（10/4 起連續失敗、而且 Telegram 完全沒通知）：
- ★【整支在 import 就死】selectolax 1.0.0（2026-10-03 發布）拿掉了 Modest backend，
  而 googlenewsdecoder 0.2.1 還在 `from selectolax.parser import HTMLParser`，
  於是 `import googlenewsdecoder` 直接丟 ImportError。那一行在 try/except 外面，
  所以連「⚠️ 今日新聞彙整失敗」都送不出來，只有 Actions 頁面是紅的。
  → workflow 釘 `selectolax<1.0`（真正的修法）。
  → 這裡把 import 改成「載不進來也照跑」：連結退回搜尋頁，並在訊息結尾加一行警告。
     連結解碼只是加分功能，不該讓整份摘要陪葬。

2026-10-07 第二輪（依 10/7 凌晨手動跑出來的那份摘要回推）：
- ★【MQG／APO／KKR 幾乎沒新聞】9/21～10/3 發出的 100 則裡 Brookfield 系 73、APO 13、MQG 3、KKR 0。
  原因在發現層的結構，不是那幾家沒新聞：
    (1) 舊版依 QUERIES 順序收，最後 out[:70] 直接截斷；Brookfield 的 7 個查詢排最前面，
        一天就能把 70 個名額吃掉大半，排第 8 之後的 Macquarie／Apollo／KKR 只撿得到剩下的。
    (2) 單一來源上限 6 則是「全體共用」，Bloomberg／Reuters 這類好來源的額度被 Brookfield 先用完。
  → QUERY_GROUPS 分群組、GROUP_QUOTA 保留名額（BN 30／MQG 18／APO 18／KKR 18／同業 10／主題 6），
    某群組沒用完的名額才讓給別人；來源上限改成「每個群組各自算」；群組內各查詢輪流取。
  → 三檔各自多加查詢（Macquarie Asset Management／Capital／Bank、Apollo Global、Athene、Marc Rowan、
    KKR & Co、Global Atlantic…）；MQG 另外多抓澳洲版 Google News。
  → prompt 明講六檔同等重要；字數上限 2500 → 3500、max_tokens 4000 → 8000。
  → 訊息結尾加一行「📊 送審→報出」各群組則數，之後哪一檔又變少，一眼看得出是沒抓到還是被篩掉。
- ★【摘要出現標題沒有的數字】「設定 $1.3 兆資產目標」「資產管理規模達 $672.2 億」都不在標題裡。
  prompt 早就禁止，還是漏 → verify_numbers()：摘要裡每個數字都要能在它引用的標題找到
  （允許 million→萬、billion→億 這種只移動小數點的換算），找不到就把那個子句刪掉；
  刪到主要子句時整句退回原標題。日期／年份不查。
- 【行事曆公告】「to Host Third Quarter Results Conference Call」一天三則 → Python 層硬刪。
- 【情緒灌水】評級「確認」、小額 LP 承諾、計畫啟動被算成偏多 → prompt 補上這三條都算中性。
- 【標錯持股】外部 LP 投資 Brookfield Infrastructure Fund 被標 BIPC → 對應表寫明私募基金募資歸 BN。
- 【只貼這一支也能跑】workflow 沒釘 selectolax 版本時，腳本在 GitHub Actions 上會自己 pip 裝回
  相容版再重啟一次；自救失敗才退回搜尋連結＋警告。workflow 那行有釘最好，沒釘也不會壞。

由 GitHub Actions 觸發。環境變數（repo Secrets）：ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
依賴：pip install requests "googlenewsdecoder>=0.2.1,<0.3" "selectolax>=0.4.12,<1.0"
      ← 兩個都要釘：0.2.x 需要 Python ≥ 3.11；selectolax 1.0 會讓 googlenewsdecoder 0.2.1 載不進來
workflow 需 `permissions: contents: write` + `concurrency:` + 跑完 commit/push sent_state.json。
"""

import os
import re
import sys
import json
import time
import datetime
import itertools
import subprocess
import email.utils
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from collections import Counter

import requests

# ── 連結解碼套件：載不進來先自救，再不行才退回搜尋連結 ──────────────
# googlenewsdecoder 0.2.1 還在用 selectolax 的 Modest backend，selectolax 1.0（2026-10-03）把它拿掉了，
# 所以沒釘版本的環境一 import 就 ImportError。2026-10-04～06 就是死在這裡，而且死在 try/except 之前，
# Telegram 完全沒收到失敗通知。
GNEWS_PINS = ["googlenewsdecoder>=0.2.1,<0.3", "selectolax>=0.4.12,<1.0"]
_SELFHEAL_FLAG = "DAILY_NEWS_SELFHEALED"

try:
    from googlenewsdecoder import gnewsdecoder
    GNEWS_IMPORT_ERROR = ""
except Exception as _e:  # ImportError 之外，套件內部初始化出錯也一樣處理
    gnewsdecoder = None
    GNEWS_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"
    print(f"⚠️ googlenewsdecoder 載入失敗：{GNEWS_IMPORT_ERROR}", file=sys.stderr)
    # 只在 GitHub Actions（用完即丟的環境）自動修；在自己電腦上不亂動別人的套件。
    # 用環境變數當旗標，保證最多只重啟一次，不會無限迴圈。
    if (
        __name__ == "__main__"
        and os.environ.get("GITHUB_ACTIONS") == "true"
        and not os.environ.get(_SELFHEAL_FLAG)
    ):
        try:
            print(f"→ 自動安裝相容版本後重啟：{GNEWS_PINS}", file=sys.stderr)
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-q",
                 "--disable-pip-version-check", *GNEWS_PINS],
                check=True, timeout=240,
            )
            os.environ[_SELFHEAL_FLAG] = "1"
            sys.stderr.flush()
            os.execv(sys.executable, [sys.executable] + sys.argv)  # 換一個乾淨的 Python 重新跑
        except Exception as _e2:
            GNEWS_IMPORT_ERROR += f"｜自動修復失敗：{type(_e2).__name__}: {_e2}"
            print(f"⚠️ 自動修復失敗，連結將退回搜尋頁：{_e2}", file=sys.stderr)

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

TAIPEI = datetime.timezone(datetime.timedelta(hours=8))
TODAY = datetime.datetime.now(TAIPEI).strftime("%Y-%m-%d")

# ── 去重 / 新鮮度設定 ────────────────────────────────────────
STATE_FILE = Path("sent_state.json")
DEDUP_DAYS = 7                # 「近期」：同一事件在這段期間內一律不再報
TITLE_MEMORY_DAYS = 90        # 標題記憶：超過 7 天的同一事件，只有新事實才以【進度更新】報
TITLE_MEMORY_LIMIT = 600      # 餵給 Claude 的已發送標題上限（約 1.5 萬 input tokens）
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
    # 波蘭 3C/SEO 內容農場（反覆灌 Macquarie 股價波動稿，與持股無實質關聯）
    "bez kabli", "bez-kabli", "bezkabli",
    # "rebel news",   # 政治立場類要不要擋自己決定
}

# ── 觀點 / 分析來源（不擋，但標記後交給 Claude 降級或略過）──────────
OPINION_SOURCES = {
    "seeking alpha", "motley fool", "simply wall st", "simplywall",
    "kalkine", "gurufocus", "traders union", "marketsmojo", "tipranks",
    "zacks", "investorplace", "wealth awesome", "newsline", "stocktwits",
    "kavout",                      # 演算法分析稿（如「…a new benchmark for…」這類軟文框架）
    "ad hoc news", "ad-hoc-news",  # 內容回收站（weekly outlook / 填充稿）；降級為只在有具體新事實時才報
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
    # 純股價波動 / 技術線型 / 每日漲跌 recap（對長線零訊號；prompt 已要求略過，這裡做硬刪雙保險）
    # 註：「% this week」較廣，理論上可能誤殺「raises distribution 5% this week」之類，
    #     但本語料幾乎只出現在 bez-kabli 的週漲跌稿；若日後發現誤殺再拿掉即可。
    "stock price forecast", "price forecast:", "resistance in focus",
    "resistance as", "trades flat near", "outperforms market", "underperforms market",
    "weekly outlook", "% this week", "shares finish week",
    # 52 週高低點稿（9/26 情緒段落引用了它，但它本身不該被報、也不該影響情緒）
    "52-week low", "52-week high", "52 week low", "52 week high",
    "new 52-week", "hits new low", "hits new high",
    # 行事曆公告：「將於某日公布財報／舉行電話會議」本身不是事件（10/7 一次報了三則）
    "results conference call", "earnings conference call", "conference call and webcast",
    "announces date of", "announces dates for", "announces timing of",
}

# 同上，但用 regex 才抓得準的樣式：
#   "X to Host Third Quarter 2026 Results Conference Call"、"KKR to Announce Third Quarter 2026 Results"
# 刻意不含 "to report"：「Macquarie to report record first-half profit」這種是前瞻報導，不是行事曆。
NOISE_TITLE_REGEXES = [
    re.compile(
        r"\bto (?:host|hold|announce|release|webcast)\b.{0,60}"
        r"\b(?:first|second|third|fourth|1st|2nd|3rd|4th|q[1-4]|full[- ]year|year[- ]end|quarterly|annual|interim|half[- ]year)\b"
        r".{0,60}\b(?:results|earnings|conference call|webcast)\b"
    ),
]

# ── 持股對應表（寫進 prompt，讓 Claude 把子公司／旗下平台標到正確的持股）──────
HOLDING_ENTITY_MAP = """\
- BN：Brookfield Corporation、Brookfield Asset Management（BAM）、Brookfield Wealth Solutions（BWS／BNT）、
  Oaktree Capital（已 100% 併入，含 Howard Marks）、Brookfield Properties／Brookfield Property Partners（BPY）、
  Brookfield Business Partners（BBU）、Brookfield 旗下各基金（轉型基金、AI 基建基金等）及其投資組合公司。
  Brookfield 管理的「私募基金」（Brookfield Infrastructure Fund／BIF、Global Transition Fund、不動產基金、
  信貸基金…）的募資、外部 LP 出資承諾，一律標 BN（那是資產管理費的來源），不要標 BIPC／BEPC
- BIPC：上市的 Brookfield Infrastructure Partners／Corporation（BIP／BIPC）本身，以及它持有或買賣的資產。
  注意「Brookfield Infrastructure Fund」是私募基金，不等於 BIPC
- BEPC：Brookfield Renewable（BEP／BEPC）及其投資組合公司（例如 Avaada、Westinghouse 相關）
- MQG：Macquarie Group、Macquarie Asset Management 及其投資組合公司
- APO：Apollo Global Management、Athene 及其投資組合公司
- KKR：KKR、Global Atlantic、FS KKR 及其投資組合公司
一則新聞同時對應多檔時全部列出，例如【持股】BN／BEPC。"""

# ── 撞名硬刪（Brookfield 作為地名/小鎮/房產/學校，與持股無關）──────────
IRRELEVANT_TITLE_PATTERNS = {
    "brookfield center", "riverside-brookfield", "town of brookfield",
    "village of brookfield", "brookfield central", "brookfield east",
    "brookfield zoo", "brookfield high", "brookfield fireworks",
    "brookfield dispensary", "brookfield shops", "brookfield, il",
    "brookfield, wi", "brookfield, wisconsin", "brookfield, illinois",
    # Macquarie 作為澳洲地名／大學／不相干的同名公司（多抓澳洲版之後會撞到）
    "macquarie university", "port macquarie", "lake macquarie", "macquarie park",
    "macquarie fields", "macquarie island", "macquarie harbour", "macquarie point",
    "macquarie street", "macquarie centre", "macquarie dictionary",
    "macquarie technology", "macquarie telecom", "macquarie data centres",
    # Apollo 撞名（印度醫院／輪胎／製藥、太空計畫、戲院）
    "apollo hospitals", "apollo tyres", "apollo micro systems", "apollo pharmacy",
    "apollo pipes", "apollo 11", "apollo 13", "apollo theater", "apollo theatre",
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

MAX_PER_SOURCE = 5            # 單一來源上限：「每個群組各自算」（舊版全體共用 6，好來源被 Brookfield 先用完）
SHOW_STATS_FOOTER = True      # 訊息結尾加一行各群組「送審→報出」則數；嫌吵改 False

# ── 發現層設定 ──────────────────────────────────────────────
# 每一列：(群組代號, 要抓的 Google News 版本, 查詢清單)
# - 有空白的查詢會自動加引號當「完整片語」；自己寫了引號或 OR 的查詢則原樣送出。
# - 群組順序＝優先順序：同一則新聞被兩個群組都搜到時，歸給排前面的群組。
# - 舊版把 20 個查詢排成一列、最後 out[:70] 截斷 → 排後面的 MQG／APO／KKR 只撿得到 Brookfield 剩下的名額。
QUERY_GROUPS = [
    ("BN", ("US",), [
        "Brookfield",
        "Brookfield Corporation",
        "Brookfield Asset Management",
        "Brookfield Infrastructure",
        "Brookfield Renewable",
        "Brookfield real estate",
        "Brookfield receiver distressed",
        "Oaktree Capital",
        "Bruce Flatt",
        "Howard Marks Oaktree",
    ]),
    ("MQG", ("US", "AU"), [          # 澳洲公司，美國版 Google News 收得少，所以多抓澳洲版
        "Macquarie Group",
        "Macquarie Asset Management",
        "Macquarie Capital",
        "Macquarie Bank",
    ]),
    ("APO", ("US",), [
        "Apollo Global Management",
        "Apollo Global",
        "Athene",
        "Marc Rowan",
    ]),
    ("KKR", ("US",), [
        "KKR",
        "KKR & Co",
        "Global Atlantic",
        "Kohlberg Kravis Roberts",
    ]),
    ("同業", ("US",), [
        "Blackstone",
        "Partners Group",
        "Ares Management",
        "Blue Owl Capital",
    ]),
    ("主題", ("US",), [
        "private credit",
        "infrastructure fund deal",
        "commercial real estate distress",
    ]),
]

# 每個群組「保留」送給 Claude 的名額；某群組沒用完的名額，再依群組順序輪流分給還有候選的群組。
# 想讓某一檔更多／更少，改這裡的數字就好（總和建議＝MAX_ITEMS）。
GROUP_QUOTA = {"BN": 30, "MQG": 18, "APO": 18, "KKR": 18, "同業": 10, "主題": 6}
MAX_ITEMS = 100               # 送給 Claude 的總則數上限（舊版 70）

WINDOW = "when:2d"
# Google News 版本：代號 → (hl, gl, ceid)
EDITIONS = {
    "US": ("en-US", "US", "US:en"),
    "AU": ("en-AU", "AU", "AU:en"),
}


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
    # 保留 TITLE_MEMORY_DAYS 天（不是 DEDUP_DAYS），跨月的舊事件才認得出來
    cutoff = (
        datetime.datetime.now(TAIPEI) - datetime.timedelta(days=TITLE_MEMORY_DAYS)
    ).strftime("%Y-%m-%d")
    pruned = {k: v for k, v in state.items() if _entry_date(v) >= cutoff}
    STATE_FILE.write_text(json.dumps(pruned, ensure_ascii=False), "utf-8")


def recent_sent_titles(
    state: dict, days: int = TITLE_MEMORY_DAYS, limit: int = TITLE_MEMORY_LIMIT
) -> list:
    """近 N 天已發送過的 (日期, 標題)（近的在前），給 Claude 做跨次語意去重用。
    帶日期是為了讓 Claude 分得出「7 天內＝一律不報」與「更早＝只有新事實才報」。"""
    cutoff = (
        datetime.datetime.now(TAIPEI) - datetime.timedelta(days=days)
    ).strftime("%Y-%m-%d")
    rows = []
    for v in state.values():
        if isinstance(v, dict) and v.get("t") and v.get("d", "") >= cutoff:
            rows.append((v["d"], v["t"]))
    rows.sort(reverse=True)
    return rows[:limit]


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
    return any(p in t for p in NOISE_TITLE_PATTERNS) or any(
        r.search(t) for r in NOISE_TITLE_REGEXES
    )


def is_irrelevant_title(title: str) -> bool:
    t = (title or "").lower()
    return any(p in t for p in IRRELEVANT_TITLE_PATTERNS)


def is_blocked_title(title: str) -> bool:
    if not BLOCKED_TITLE_PATTERNS:
        return False
    t = (title or "").lower()
    return any(p in t for p in BLOCKED_TITLE_PATTERNS)


# ── 發現層 ─────────────────────────────────────────────────
def fetch_rss(query: str, edition: str = "US") -> list:
    hl, gl, ceid = EDITIONS[edition]
    if '"' in query or " OR " in query:
        phrase = query                                   # 自己寫好運算子的查詢：原樣送出
    else:
        phrase = f'"{query}"' if " " in query else query
    q = urllib.parse.quote(f"{phrase} {WINDOW}")
    url = f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"
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
                    "source": ((src_el.text if src_el is not None else "") or "").strip(),
                    "q": query,
                }
            )
        return items
    except Exception as e:
        print(f"RSS fail [{edition}:{query}]: {e}", file=sys.stderr)
        return []


def collect(sent_state: dict):
    """回傳 (items, stats)。
    每個群組：來源/噪音/撞名/主題過濾 → 同次去重 → 跨日(精確)去重 → 48h fail-closed
              → 群組內各查詢輪流取（不讓單一查詢佔滿）→ 群組內單一來源上限。
    最後依 GROUP_QUOTA 保留名額，沒用完的名額再輪流補給其他群組，總數不超過 MAX_ITEMS。
    stats[群組] = {"raw": RSS 原始則數, "pool": 過濾後候選, "picked": 實際送給 Claude}"""
    seen = set()
    order = [g for g, _, _ in QUERY_GROUPS]
    pools, stats = {}, {}

    for group, editions, queries in QUERY_GROUPS:
        per_query, raw = [], 0
        for q in queries:
            for ed in editions:
                got = []
                for it in fetch_rss(q, ed):
                    raw += 1
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
                    it["key"], it["group"] = key, group
                    got.append(it)
                per_query.append(got)
                time.sleep(0.5)

        # 群組內：各查詢的第 1 則、各查詢的第 2 則…輪流取，順便套「群組內」單一來源上限
        pool, per_source = [], Counter()
        for row in itertools.zip_longest(*per_query):
            for it in row:
                if it is None:
                    continue
                s = it["source"].lower()
                if MAX_PER_SOURCE and per_source[s] >= MAX_PER_SOURCE:
                    continue
                per_source[s] += 1
                pool.append(it)
        pools[group] = pool
        stats[group] = {"raw": raw, "pool": len(pool), "picked": 0}

    # 先各拿各的保留名額
    picked = {g: pools[g][: GROUP_QUOTA.get(g, 0)] for g in order}
    rest = {g: pools[g][GROUP_QUOTA.get(g, 0):] for g in order}
    total = sum(len(v) for v in picked.values())
    # 有群組沒用完 → 剩下的名額依群組順序一則一則輪流補
    while total < MAX_ITEMS and any(rest.values()):
        for g in order:
            if total >= MAX_ITEMS:
                break
            if rest[g]:
                picked[g].append(rest[g].pop(0))
                total += 1

    out = []
    for g in order:
        stats[g]["picked"] = len(picked[g])
        out.extend(picked[g])
    return out[:MAX_ITEMS], stats


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
2. 對我的持股（BN／BIPC／BEPC／MQG／APO／KKR）有直接影響的放最前面，標記【持股】並寫出對應代號。
   標題只寫子公司或旗下平台名稱時，照下面的對應表判斷屬於哪一檔（例如 Brookfield Renewable → BEPC、Oaktree → BN）：
{HOLDING_ENTITY_MAP}
   六檔持股同等重要：MQG、APO、KKR 的公司層級實質事件（併購／出售／入股、募資完成、財報與指引、評等變動、
   監管與訴訟裁定、高層人事、配息／回購）和 Brookfield 的一樣要報，不要因為 Brookfield 相關的則數多就把它們擠掉。
   但也不要為了湊數放寬標準：某一檔今天沒有實質事件，就不報那一檔。
3. 摘要長度由標題實際資訊量決定：標題只講一件事，就用「一句話」照實複述；不要為了湊到 2-3 句而補上標題沒有的背景、動機、影響或解讀。只有標題本身就含多個事實時才寫到 2-3 句。寧可短，不要腦補。
4. 優先呈報：實體資產出售、商用不動產壞帳/接管、併購與重組進度、評等與展望變動、配息/回購政策、旗艦基金募資與贖回(gate)、管理階層(如 Bruce Flatt、Howard Marks)發言或合作。
5. 沒有重大新聞的板塊直接略過。若清單裡確實沒有重要的，就誠實說「今日無重大新聞」。
6. 結尾給一句「今日板塊情緒：偏多／中性／偏空」並簡述理由。情緒規則：
   - 只能根據「這次有報出來」的新聞判斷；清單裡有、但你沒報的項目（股價漲跌、52 週高低點、被略過的稿子）一律不准拿來當理由。
   - 依確定性加權：已完成／已交割／已公布財報 > 排他談判 > 非約束性承諾或傳聞 > 會面、計劃、「洽談中」。後兩類最多只能算微弱訊號。
   - 依對持股的直接程度加權，不是依則數：同一天好幾筆小交易不等於偏多。
   - 【觀點】與【進度更新】不計入情緒。
   - 評級「確認／維持」（affirm、reaffirm、maintain）是維持現狀，算中性，不是利多；只有升評、降評或展望改變才算訊號。
   - 單一 LP 的出資承諾、相對於該管理人規模微不足道的小額交易，算中性，不是利多。
   - 「啟動／推出／計畫／launch／drive／plan」這類還沒有成交或金額的消息，算中性。
   - 報出來的新聞裡，若沒有至少一則「已完成或已公布、且對持股有實質影響」的事件，情緒就寫中性。
   - 理由只講新聞內容本身，不要提「清單」「可計入」「略過」「不計入」這類描述你篩選過程的字眼。

【不准腦補敘事・最重要】
- 除了標題字面明確寫出的事實，一律不准推論：不准推測公司動機、策略意圖、對股價或估值的影響、與其他公司的比較或分歧、交易背後的原因。
- 標題若是比喻、雙關或語意不明（例如「X reaches for orbit as Y heads for the exit」這種把兩件事並列的標題），只照字面說標題提到了什麼，絕對不要把兩件事連成因果、對比或「同一件事的兩面」，也不要自己編一個解釋。看不懂就只複述字面。
- 標題沒有的金額、估值、股票代號、EPS 數字、人名，一律不准自己編；不確定的實體寧可說「未揭露」。
- 數字只能用標題裡出現的數字：資產管理規模、目標、估值、持股比例、殖利率這類數字，標題沒有就不要寫。
  可以把 million／billion 寫成中文的萬／億（同一幣別、只移動小數點，例如 $30 Mln → 3,000 萬美元），
  但不能出現標題沒有的數字。程式會逐一比對，對不上的數字所在的句子會被刪掉。
- 不要自己做單位或貨幣換算：標題寫 crore／lakh／億／美元就照標題原文寫，不要換成其他幣別或單位（例如不要把「INR 3,656 crore」寫成「3,656 億盧比」，也不要自行折算成美元）。印度單位 crore＝千萬、lakh＝十萬，換算極易出錯，一律照原文。

【只輸出成品，不要解釋過程・最重要】
- 只輸出最後選出來要報的新聞。不要解釋你過濾了什麼、跳過了什麼、為什麼跳過、哪些和近期已發送重複。
- 禁止出現這類句子：「以下為今日無新增價值的項目」「此筆已多次發送，全數略過」「（注：…已於近期發送清單中已報…）」「已發送過」「未重複已發送內容」。
- 該略過的就靜默略過，連提都不要提；輸出裡不該有任何關於「去重／過濾／清單」的後設說明。

【避免重複】
- 下面「已發送紀錄」是過去 {TITLE_MEMORY_DAYS} 天報過的標題，每行開頭有發送日期。
- 發送日期在最近 {DEDUP_DAYS} 天內的：今天清單若有一則和它其實是【同一件事】（同一筆交易／財報／報告／訴訟／13F／合作案），即使用字、角度、數字、來源不同，一律不要再報，也不要說明你略過了它。
- 發送日期早於 {DEDUP_DAYS} 天的：同一件事只有在標題出現「新的具體事實」時才報（例如從洽談變成簽約、完成交割、金額或條件改變、監管裁定）。要報就標【進度更新】，一句話寫出新增的那個事實，不計入情緒。標題看不出新事實的，靜默略過。
- 【進度更新】的三個硬條件，缺一就靜默略過：
  (a) 來源不是［觀點來源］——分析稿、評論稿永遠不能當進度更新；
  (b) 標題描述的是「剛發生的動作」（signs、completes、approves、raises bid、rules、files），不是「目前的狀態」或「可能性」（deal agreed but…、could a rival…、what's next for…）；
  (c) 這個事實不在已發送紀錄裡以任何說法出現過。
- 【進度更新】那一則只寫新事實本身，不准加任何說明為什麼它是進度更新的括號或註記（例如「核心事件先前已報」「新事實：…」都不准出現）。
- 即使不在紀錄裡，若標題明顯是在追述較早的事件（例如「繼 7 月宣布後…」「推進先前的…」），同樣視為【進度更新】處理，不要當成今天的新事件。
- 今天清單內部若有多則是同一件事，只挑資訊量最高的一則報，其餘靜默併入或捨棄。
- 特別注意「跟進報導」：一筆大型交易（例如某子公司出售案）成交後，接下來幾天會有大量不同媒體報導同一件事。只要這筆交易你先前已經報過，之後所有跟進報導一律直接不報，不論它是不是我的持股、不論又有幾家媒體跟進。不要「報一則然後加註『無新增事實／跟進報導』」——那還是重複。判斷標準只有一個：核心事件先前報過沒有？報過就丟，連提都不要提。

【觀點來源處理】
- 標記［觀點來源］的是分析/評論稿（Seeking Alpha、Motley Fool、Simply Wall St 等），不是第一手新聞。
- 這類只有在「提供了清單裡其他第一手新聞沒有、且具體的新事實」時才報；否則一律略過。
- 若真要報，標記改用【觀點】（不要用【持股】），並寫明這是第三方分析。
- 資產管理公司「自己發布」的市場展望、研究報告、投資觀點（例如 Apollo、KKR、Oaktree、Brookfield 的 outlook／insights／memo）同樣屬於觀點，不是公司事件：只有在內容本身就是具體新事實時才報，並標【觀點】；否則略過。

【排除噪音】
- 跳過原告律所的「股東警示／集體訴訟召集／investigation」樣板稿，除非有具體且重大的法律進展（正式起訴、和解金額、法院裁定）。
- 跳過例行 13F／持倉增減披露，除非對「我的持股本身」具策略意義。
- 跳過行事曆公告：「將於某日公布財報」「將舉行業績電話會議／webcast」「公布股東會日期」本身不是事件，不要報。

【網址處理】
- 不要自己貼網址或寫時間，這些我會用程式補上。
- 摘要內文不要寫出來源網域名（例如 thedeal.com、marketscreener.com），那是給程式用的，寫進內文會被自動連結化弄壞。
- 每則摘要最後放對應標記 [[編號]]，編號就是清單該則開頭 [數字] 的數字。
- 多則併成同一則時，把編號全部相連放結尾，例如 [[1]][[5]][[8]]；不同新聞要分開。

輸出格式：純文字，適合 Telegram。每則之間空一行。開頭寫上日期。整體 3500 字以內。

──── 已發送紀錄（過去 {TITLE_MEMORY_DAYS} 天，格式「發送日期｜標題」，請務必拿來比對去重）────
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
            "max_tokens": 8000,
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
    # 1b) 去掉說明「為什麼算進度更新」的括號，例如（核心事件先前已報，新事實：…）
    text = re.sub(r"[（(][^）)]*(?:先前已報|已報過|先前報過|新事實[：:])[^）)]*[）)]", "", text)
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


# ── 數字查核（摘要裡的數字必須出自它引用的標題）────────────────────
# Claude 只看得到標題，所以摘要裡任何「標題沒有的數字」一定是編的（10/7：$1.3 兆、$672.2 億）。
# 做法：以 [[編號]] 為界切段 → 每段的數字去對那段引用的標題 → 對不上的子句刪掉。
# 允許「只移動小數點」的換算（$30 Mln → 3,000 萬、$28b → 280 億），所以比對的是去掉頭尾 0 的有效數字。
# 已知的寬鬆處（寧可放過、不要誤刪）：日期與年份不查；沒有單位的個位數不查（Q3、3 家）；
# 有效數字相同就放行（標題有 30，摘要寫 3 億也會過）。
MARKER_RUN = re.compile(r"\[\[\d+\]\](?:[\s,，、]*\[\[\d+\]\])*")
_NUM_RE = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")
_DATE_RE = re.compile(
    r"(?:19|20)\d{2}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2}"          # 2026-10-07
    r"|(?:19|20)\d{2}\s*年(?:\s*\d{1,2}\s*月)?(?:\s*\d{1,2}\s*[日號])?"  # 2026 年 10 月 7 日
    r"|\d{1,2}\s*月\s*\d{1,2}\s*[日號]"                               # 10 月 7 日
    r"|\d{1,2}\s*月(?=份|底|初|中)"                                     # 10 月底
)
_UNIT_AFTER = re.compile(
    r"\s*(?:%|％|兆|億|萬|千|百萬|美元|美金|歐元|英鎊|澳幣|澳元|加幣|加元|日圓|日元|港幣|港元|盧比|人民幣|元|倍"
    r"|個?基點|bps?\b|trillion|billion|million|mln|bln|bn\b|[mbk]\b|crore|lakh|GW|MW|吉瓦)",
    re.I,
)
_CUR_BEFORE = re.compile(r"(?:US\$|A\$|C\$|HK\$|NT\$|\$|€|£|¥|₹|Rs\.?|INR|USD|AUD|CAD|EUR|GBP)\s*$", re.I)
_CLAUSE_SPLIT = re.compile(r"((?<!\d),(?!\d)|[，；;。])")
_LABEL_RE = re.compile(r"^\s*(?:【[^】]*】\s*(?:[A-Z]{2,5}(?![A-Za-z])(?:\s*[／/、]\s*[A-Z]{2,5}(?![A-Za-z]))*)?\s*)?")
_WORD_NUMS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12",
    "first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5",
    "double": "2", "doubles": "2", "doubled": "2", "doubling": "2",
    "triple": "3", "triples": "3", "tripled": "3",
    "half": "50", "halves": "50", "halved": "50", "dozen": "12",
}


def _mantissa(tok: str) -> str:
    """去掉逗號、小數點與頭尾的 0：30／3,000／0.3 都是 "3"；15.06 是 "1506"。"""
    return tok.replace(",", "").replace(".", "").strip("0") or "0"


def _glued_to_letter(text: str, pos: int) -> bool:
    """數字前面緊貼英文字母（Q3、H1、A320、COP28）＝代號的一部分，不是數量。"""
    return pos > 0 and text[pos - 1].isascii() and text[pos - 1].isalpha()


def _title_numbers(titles: list):
    """回傳 (原樣數字集合, 有效數字集合)。"""
    exact, mant = set(), set()
    for t in titles:
        for m in _NUM_RE.finditer(t):
            tok = m.group(0).replace(",", "")
            exact.add(tok)
            if not _glued_to_letter(t, m.start()):   # Q3 的 3 不拿來放行「3 億」
                mant.add(_mantissa(tok))
        for w in re.findall(r"[a-z]+", t.lower()):
            if w in _WORD_NUMS:
                exact.add(_WORD_NUMS[w])
    return exact, mant


def _bad_numbers(text: str, exact: set, mant: set) -> list:
    """text 裡對不上標題的數字（日期不算）。"""
    scrub = _DATE_RE.sub(lambda m: " " * len(m.group(0)), text)
    bad = []
    for m in _NUM_RE.finditer(scrub):
        s, e = m.span()
        if _glued_to_letter(scrub, s):
            continue
        tok = m.group(0).replace(",", "")
        has_unit = bool(_UNIT_AFTER.match(scrub, e)) or bool(_CUR_BEFORE.search(scrub[:s]))
        if len(tok) == 1 and not has_unit:           # 沒單位的個位數（3 家、第 2 次）不查
            continue
        if tok in exact or _mantissa(tok) in mant:
            continue
        bad.append(m.group(0))
    return bad


def _scrub_line(line: str, exact: set, mant: set, fallback_title, protect: str = ""):
    """刪掉含有對不上數字的子句。回傳 (新的一行, 動了幾處)。
    第一個子句（通常是主詞＋動作）被刪 → 整行退回原標題；沒有原標題可退（情緒段）就整行拿掉。"""
    bad_all = _bad_numbers(line, exact, mant)
    if not bad_all:
        return line, 0
    body = line.strip()
    lead = line[: len(line) - len(line.lstrip())]
    trail = line[len(line.rstrip()):]
    label = _LABEL_RE.match(body).group(0)
    clauses = [c.strip() for c in _CLAUSE_SPLIT.split(body[len(label):])[::2] if c.strip()]
    kept, removed, first_removed = [], 0, False
    for i, c in enumerate(clauses):
        if _bad_numbers(c, exact, mant) and not (protect and protect in c):
            removed += 1
            first_removed = first_removed or i == 0
            continue
        kept.append(c)
    if not removed:
        return line, 0
    print(f"數字查核：標題沒有 {bad_all} → 處理「{body[:60]}」", file=sys.stderr)
    if first_removed or not kept:
        if fallback_title:
            return f"{lead}{label}〔原標題〕{fallback_title}{trail}", 1
        return "", 1
    new = "，".join(kept)
    if body.endswith("。") and not new.endswith("。"):
        new += "。"
    return f"{lead}{label}{new}{trail}", removed


def verify_numbers(digest: str, items: list):
    """回傳 (查核後的文字, 動了幾處)。[[編號]] 標記本身完全不動。"""
    idx = {str(i): it for i, it in enumerate(items, 1)}
    out, pos, fixes, all_titles = [], 0, 0, []
    matches = list(MARKER_RUN.finditer(digest))
    for m in matches:
        titles = [
            idx[n]["title"] for n in re.findall(r"\[\[(\d+)\]\]", m.group(0)) if n in idx
        ]
        all_titles += titles
        seg = digest[pos:m.start()]
        if titles:
            exact, mant = _title_numbers(titles)
            fb = re.sub(r"\s+[\-\–\—\|]\s+[^\-\–\—\|]+$", "", titles[0])   # 去掉結尾「 - 來源名」
            lines = []
            for ln in seg.split("\n"):
                new, n = _scrub_line(ln, exact, mant, fb)
                lines.append(new)
                fixes += n
            seg = "\n".join(lines)
        out += [seg, m.group(0)]
        pos = m.end()
    tail = digest[pos:]
    if matches:   # 最後一個標記之後＝情緒段：數字要出自「這次有報出」的任何一則標題
        exact, mant = _title_numbers(all_titles)
        lines = []
        for ln in tail.split("\n"):
            new, n = _scrub_line(ln, exact, mant, None, protect="板塊情緒")
            lines.append(new)
            fixes += n
        tail = "\n".join(lines)
    out.append(tail)
    return re.sub(r"\n{3,}", "\n\n", "".join(out)), fixes


# ── 連結還原（修死連結）─────────────────────────────────────
DECODE_STATS = Counter()


def _gnews_decode(article_url):
    """用 googlenewsdecoder 還原 Google News 加密轉址。失敗回 None，並把原因印到 log。
    ★ 0.1.x 回傳 {"status": True, "decoded_url": ...}；0.2.x 改成 {"success": True, ...}。
      舊版只認 "status"，升到 0.2.x 後每則都被判失敗 → 全部退回搜尋連結（9 月下旬的症狀）。
      這裡兩種都認；workflow 也請把版本釘住，避免下次又被無聲改版。"""
    if gnewsdecoder is None:                 # 套件沒載進來（見檔頭 import 區）
        DECODE_STATS["unavailable"] += 1
        return None
    try:
        res = gnewsdecoder(article_url, interval=1)
    except Exception as e:
        DECODE_STATS["exception"] += 1
        print(f"decode exception: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    ok = bool(res.get("success") or res.get("status"))
    url = str(res.get("decoded_url") or "")
    if ok and url.startswith("http"):
        DECODE_STATS["ok"] += 1
        return url
    DECODE_STATS["fail"] += 1
    print(f"decode fail: {res.get('message') or res.get('error') or res}", file=sys.stderr)
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
        host = urllib.parse.urlparse(final).hostname or ""
        # 任何 google.com 網域（news / sorry / consent）都不是文章本身
        if final.startswith("http") and not (host == "google.com" or host.endswith(".google.com")):
            return final
    except Exception:
        pass

    # 3) 一定打得開的退路
    return "https://news.google.com/search?q=" + urllib.parse.quote(title or google_link)


def inject_links(digest: str, items: list):
    """把 [[編號]] 換成(還原後的)網址。同段多個來源只顯示第一個，但全部記為已發送。"""
    idx = {str(i): it for i, it in enumerate(items, 1)}
    sent_keys = set()
    # 容許 [[1]][[5]]、[[1]] [[5]]、[[1]], [[5]]、[[1]]、[[5]] 都算同一串（與 verify_numbers 共用 MARKER_RUN）
    run = MARKER_RUN

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
        items, stats = collect(sent_state)
        order = [g for g, _, _ in QUERY_GROUPS]
        print(f"抓到 {len(items)} 則（噪音/來源/撞名過濾 + 精確去重 + 48h + 群組配額後）", file=sys.stderr)
        print(
            "各群組 RSS原始→候選→送審："
            + "｜".join(
                f"{g} {stats[g]['raw']}→{stats[g]['pool']}→{stats[g]['picked']}" for g in order
            ),
            file=sys.stderr,
        )

        if sum(stats[g]["raw"] for g in order) == 0:
            # 每個查詢都 0 則＝抓取本身壞了（被 Google 擋、網路問題），不要誤報成「今天沒新聞」
            raise RuntimeError("所有 Google News 查詢都沒有回傳任何結果（抓取失敗，不是沒新聞）")

        if not items:
            send_telegram(f"📅 {TODAY}\n今日近 48 小時內沒有新的（未發送過的）新聞。")
            save_state(sent_state)
            sys.exit(0)

        recent_block = (
            "\n".join(f"- {d}｜{t}" for d, t in recent_sent_titles(sent_state)) or "（無）"
        )
        digest = summarize(build_news_block(items), recent_block)
        digest = strip_meta_commentary(digest)          # 送出前剝掉殘留的後設說明
        digest, num_fixes = verify_numbers(digest, items)   # 標題沒有的數字 → 刪子句／退回原標題
        final_text, sent_keys = inject_links(digest, items)

        footer = []
        if SHOW_STATS_FOOTER:
            reported = Counter(it["group"] for it in items if it["key"] in sent_keys)
            footer.append(
                "📊 送審→報出："
                + "｜".join(f"{g} {stats[g]['picked']}→{reported[g]}" for g in order)
            )
        if num_fixes:
            footer.append(f"🔎 數字查核：移除 {num_fixes} 處標題沒有的數字")
        if GNEWS_IMPORT_ERROR:
            # 讓我在 Telegram 上就看得到，不用等到哪天想起來去翻 Actions
            footer.append(
                "⚠️ 連結解碼套件載入失敗，本次連結皆為搜尋頁。原因：" + GNEWS_IMPORT_ERROR[:200]
            )
        if footer:
            final_text += "\n\n" + "\n".join(footer)
        send_telegram(final_text)

        # 記下這次「實際發出去」的標題（含標題本身），下次才能做跨次語意去重
        title_by_key = {it["key"]: it["title"] for it in items}
        for k in sent_keys:
            sent_state[k] = {"d": TODAY, "t": title_by_key.get(k, "")}
        save_state(sent_state)

        print(f"Sent OK；本次標記 {len(sent_keys)} 則為已發送；數字查核動了 {num_fixes} 處", file=sys.stderr)
        print(f"連結解碼：{dict(DECODE_STATS)}", file=sys.stderr)
        if DECODE_STATS["ok"] == 0 and (DECODE_STATS["fail"] or DECODE_STATS["exception"]):
            print("⚠️ 本次沒有任何連結解碼成功，全部是搜尋頁退路；請看上面的 decode fail 訊息。",
                  file=sys.stderr)
    except Exception as e:
        try:
            send_telegram(f"⚠️ 今日新聞彙整失敗：{e}")
        except Exception:
            pass
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)   # 讓 GitHub Actions 把這次 run 標記成失敗
