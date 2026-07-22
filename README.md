# KariKari AI Ops Bot

Discord 作為操作入口、Notion 作為營運資料層的藝人管理公司排程與任務 bot。  
KariKari 是第一個測試站；專案方向是可套用到多家公司，而不是一次性客製。

## 主流程

目前主流程是：

`/時間收集建立` → 藝人填月度可配合時段 → `/時間池比對` → 直接建立課程 Task 或由控方建立其他 Project / Task

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
| [SUBMISSION_FLOW.md](SUBMISSION_FLOW.md) | 任務提交方式、Discord 上傳限制、10MB 分界、審核通知 |
| [OPERATIONS_PLAYBOOK.md](OPERATIONS_PLAYBOOK.md) | 控方營運 SOP |
| [NOTION_SCHEMA.md](NOTION_SCHEMA.md) | Notion 資料庫交互邏輯 |
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
| `SCHEDULE_CHANNEL_ID` | 時間資訊彙整頻道 ID（公告與時間池互動處） |
| `ADMIN_ROLE_ID` | 控方 / 管理者身分組 ID（管理指令權限） |
| `TASK_CHANNEL_ID` | 任務查詢入口頻道 ID（留空則不限制任務指令頻道） |
| `ADMIN_NOTIFY_CHANNEL_ID` | 管理通知頻道 ID（留空則用排課頻道） |
| `ANNOUNCE_CHANNEL_ID` | 公告區頻道 ID（留空則公告發到時間資訊彙整；建議設唯讀頻道） |
| `BULLETIN_CHANNEL_ID` | Notion 布告欄同步到的 Discord 頻道 ID（留空則使用公告區） |
| `NOTION_TOKEN` | Notion → Settings → Integrations 建立的 internal integration secret |
| `NOTION_DS_BULLETIN` | KariKari Testing「佈告欄／留言板／使用回饋」Data Source ID；預設已內建 |

這些 ID 都是租戶設定：換到其他公司時，頻道名稱 / 用途可以維持一致，但實際 Discord ID、role ID、Notion Data Source ID 會改。  
KariKari 的 Data Source ID 已內建在 `config.py`；Notion 端重建資料庫或換租戶時，才需要在 `.env` 覆寫 `NOTION_DS_*`。

## 選填環境變數

| 變數 | 說明 |
|---|---|
| `GOOGLE_DRIVE_ROOT_FOLDER_ID` | 啟用 Discord 小檔自動歸檔到公司 Google Drive |

## Discord / Notion 前置設定

- 邀請連結 scopes：`bot` + `applications.commands`
- Bot 權限：View Channels、Send Messages、Embed Links、Mention Everyone（點名用）
- 控方成員需具備 Discord Administrator 權限，才會看到管理端 slash commands
- 建議分兩個頻道：時間資訊彙整放時間池公告與比對；任務查詢入口放 `/我的任務`、`/任務總覽` 與任務提醒
- Notion integration 要能讀寫：Artists、Availability Slots、Tasks、Projects、People、Submissions、Audit Log
- Artists 的可排課藝人要填 `Discord User ID`，且 `合約狀態` 不要是 `暫停` / `封存`

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
  scheduling_admin.py     # /時間收集建立 /時間池比對 /時間池整理
  scheduling_artist.py    # 填寫按鈕 + Modal + /我的時段 /補填時間
  tasks_artist.py         # /我的任務 /任務總覽
  submissions.py          # /提交任務 /審核提交
  reminders.py            # 月度時間池催收與任務提醒（每 30 分鐘）
tests/                    # 純函式、互動邏輯、資料層測試
```
