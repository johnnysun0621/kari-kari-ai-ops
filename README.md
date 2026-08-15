# KariKari AI Ops Bot

Discord 作為操作入口、Notion 作為營運資料層的藝人管理公司排程與任務 bot。  
KariKari 是第一個測試站；專案方向是可套用到多家公司，而不是一次性客製。

## 主流程

目前主流程是：

`/時間資訊蒐集` → 藝人填月度可配合時段 → `/從時間池建立任務` → 建立課程 Task／其他安排草稿 → 控方在 Notion 填正式時間並勾選 `Discord 顯示=true` → Bot 自動通知藝人

課程與作業、錄音、通告與其他正式事項都以 `Tasks` 為唯一安排來源。

## 常用文件

| 文件 | 用途 |
|---|---|
| [HANDOFF.md](HANDOFF.md) | AI / 工程師協作者接手指南 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 工程架構與模組責任 |
| [TESTING.md](TESTING.md) | 自動測試、Discord 驗收、Notion 驗收 |
| [FLOWCHART.md](FLOWCHART.md) | Mermaid 主流程圖與資料庫責任分界 |
| [DISCORD_CHANNELS.md](DISCORD_CHANNELS.md) | Discord 頻道組成與 slash command 對應 |
| [DISCORD_COMMANDS.md](DISCORD_COMMANDS.md) | Discord slash command 使用手冊 |
| [SUBMISSION_FLOW.md](SUBMISSION_FLOW.md) | 任務提交方式、Drive 資料夾、審核通知 |
| [OPERATIONS_PLAYBOOK.md](OPERATIONS_PLAYBOOK.md) | 控方營運 SOP |
| [NOTION_SCHEMA.md](NOTION_SCHEMA.md) | Notion 資料庫交互邏輯 |
| [NOTION_VIEWS_AUDIT.md](NOTION_VIEWS_AUDIT.md) | KariKari Testing 各區與 view 稽核 |
| [REQUIREMENTS_TRACEABILITY.md](REQUIREMENTS_TRACEABILITY.md) | 三次會議需求、功能與測試對照 |
| [SERVER_MIGRATION.md](SERVER_MIGRATION.md) | Discord 正式伺服器初始化與平轉 SOP |
| [DELIVERY.md](DELIVERY.md) | v1.0 交付範圍、限制與回復方式 |
| [PROJECT_PRINCIPLES.md](PROJECT_PRINCIPLES.md) | 產品原則與平展化方向 |
| [ROADMAP.md](ROADMAP.md) | 開發進度與下一步 |
| [RESEARCH.md](RESEARCH.md) | 需求研究與歷史脈絡 |

## 安裝

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    Mac: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填入下方設定
python bot.py
```

啟動時 bot 會自動做 Notion smoke test：讀 Artists、寫一筆 Audit Log，console log 會顯示結果。

## 必填環境變數

| 變數 | 說明 |
|---|---|
| `DISCORD_TOKEN` | Discord Developer Portal → Bot → Token |
| `GUILD_ID` | 目前租戶 / 公司的 Discord 伺服器 ID |
| `SCHEDULE_CHANNEL_ID` | 時間資訊彙整頻道 ID（藝人回報與管理可配合時間） |
| `ADMIN_ROLE_ID` | 控方 / 管理者身分組 ID（管理指令權限） |
| `TASK_CHANNEL_ID` | 藝人任務查詢入口頻道 ID（留空則不限制 `/我的任務` 頻道） |
| `AI_CHANNEL_ID` | 課程 AI 助教頻道 ID（設定後只在此頻道接受自然對話） |
| `ADMIN_NOTIFY_CHANNEL_ID` | 管理通知頻道 ID（營運內部通知；留空則用排課頻道） |
| `ADMIN_COMMAND_CHANNEL_ID` | 管理操作頻道 ID（營運操作；留空則沿用管理通知頻道） |
| `ANNOUNCE_CHANNEL_ID` | 公告區頻道 ID（留空則公告發到時間資訊彙整；建議設唯讀頻道） |
| `BULLETIN_CHANNEL_ID` | Notion 布告欄同步到的 Discord 頻道 ID（留空則使用公告區） |
| `NOTION_TOKEN` | Notion → Settings → Integrations 建立的 internal integration secret |
| `NOTION_DS_BULLETIN` | KariKari Testing「佈告欄／留言板／使用回饋」Data Source ID；預設已內建 |
| `NOTION_DS_COURSE_MEETINGS` | `課程與會議記錄` Data Source ID；預設已內建 |
| `LLM_API_KEY` | 課程助教使用的 LLM API key；留空則停用回答 |
| `LLM_BASE_URL` | LLM API base URL，預設 `https://api.openai.com/v1`；LongCat 使用 `https://api.longcat.chat/openai/v1` |
| `LLM_MODEL` | LLM 模型名稱，預設 `gpt-4o-mini` |
| `LLM_MAX_TOKENS` | 單次課程助教回答上限，預設 `800` |
| `LLM_DAILY_TOKEN_LIMIT` | 每租戶每日 token 護欄；目前預設 `0`（不限），使用量仍只記錄數字 |
| `LLM_PER_ARTIST_DAILY_TOKEN_LIMIT` | 每位藝人每日 token 護欄；目前預設 `0`（不限） |

KariKari 測試環境目前不啟用 Bot 端每日 token 上限。LLM 供應商帳戶本身的額度、速率限制與服務狀態仍然適用。

這些 ID 都是租戶設定：換到其他公司時，頻道名稱 / 用途可以維持一致，但實際 Discord ID、role ID、Notion Data Source ID 會改。  
KariKari 的 Data Source ID 已內建在 `config.py`；Notion 端重建資料庫或換租戶時，才需要在 `.env` 覆寫 `NOTION_DS_*`。

## 選填環境變數

| 變數 | 說明 |
|---|---|
| `GOOGLE_DRIVE_ROOT_FOLDER_ID` | 啟用任務提交與老師回饋的公司 Google Drive 資料夾 |

## Discord / Notion 前置設定

- 邀請連結 scopes：`bot` + `applications.commands`
- Bot 權限：View Channels、Send Messages、Embed Links、Mention Everyone（點名用）
- 控方成員需具備 Discord Administrator 權限，才會看到管理端 slash commands
- 可由控方執行 `/伺服器初始化` 建立六用途頻道與 `藝人`／`控方` 身分組；詳細步驟見 [SERVER_MIGRATION.md](SERVER_MIGRATION.md)
- Notion integration 要能讀寫：Artists、Availability Slots、Tasks、Projects、People、Submissions、Audit Log
- Artists 的可用藝人帳號要填 `Discord User ID`，且 `合約狀態` 為 `已簽約` 或 `洽談中`

## 測試

```bash
.venv/bin/python -m compileall bot.py cogs services tests config.py web
.venv/bin/python -m pytest tests/ -q
```

## 專案結構

```
bot.py                    # 進入點：載入 cogs、sync 指令、persistent 按鈕、啟動 smoke test
config.py                 # .env、Notion Data Source ID、時區/SLA 常數
services/
  notion_api.py           # Notion REST 封裝（2025-09-03，含重試）
  repositories.py         # Notion 資料庫 CRUD（中文欄位名 → API payload 唯一對應處）
  overlap.py              # 共同時段交集演算法（純函式）
  timeparse.py            # Modal 輸入解析、候選時段文字格式
  audit.py                # Audit Log 寫入（失敗不阻斷主流程）
cogs/
  scheduling_admin.py     # /時間資訊蒐集 /從時間池建立任務
  scheduling_artist.py    # 填寫按鈕 + Modal + /管理可配合時間
  tasks_artist.py         # /我的任務 /任務總覽 /任務通知重送
  submissions.py          # /我的任務內的提交操作 + /審核提交
  bulletin.py             # /布告欄同步 + Notion 公告背景同步
  course_qa.py            # 課程助教頻道自然對話
  operations_qa.py        # /營運查詢（控方限定）
  server_setup.py         # /伺服器初始化（角色、頻道、env 清單）
  reminders.py            # 月度時間池、排課 SLA、任務與審核提醒
tests/                    # 純函式、互動邏輯、資料層測試
```
