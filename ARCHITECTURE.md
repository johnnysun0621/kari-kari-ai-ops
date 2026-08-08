# 工程架構

這份文件給工程師與 AI 協作者理解程式切分方式。  
操作 SOP 看 [OPERATIONS_PLAYBOOK.md](OPERATIONS_PLAYBOOK.md)，Discord 指令看 [DISCORD_COMMANDS.md](DISCORD_COMMANDS.md)，Notion 欄位看 [NOTION_SCHEMA.md](NOTION_SCHEMA.md)。

## 系統定位

KariKari AI Ops Bot 是一個 Discord 操作入口，加上 Notion 營運資料層。

目前主流程：

`/時間資訊蒐集` → 藝人填月度可配合時段 → `/從時間池建立任務` → 控方選安排類型 → 課程走老師確認；錄音／通告／其他建立隱藏 Task 草稿 → 控方填正式時間並勾選 `Discord 顯示=true` → Bot 自動通知

老師確認表單是課程 Task 流程的一部分：

`/從時間池建立任務` 選候選 → 課程直接建立 `Tasks`，其他安排建立隱藏 Task 草稿

## 核心資料邊界

| 資料 | 定位 |
|---|---|
| `Availability Slots` | 藝人回填的可配合時間；不是正式行程 |
| `Tasks` | 正式任務與正式安排的主要來源 |
| `Projects` | 上層歸屬與提醒來源 |
| `Audit Log` | 操作紀錄與排查線索 |

## 入口檔

### `bot.py`

負責：

- 建立 `discord.py` bot。
- 載入 cogs。
- 註冊 persistent buttons。
- 同步 slash commands。
- 啟動時做 Notion smoke test：讀 `Artists`，寫 `Audit Log`。

載入的 cogs：

- `cogs.scheduling_admin`
- `cogs.scheduling_artist`
- `cogs.tasks_artist`
- `cogs.submissions`
- `cogs.reminders`
- `cogs.course_qa`
- `cogs.operations_qa`
- `cogs.server_setup`

## 設定層

### `config.py`

`TenantConfig` 是一間公司 / 租戶的設定形狀。

包含：

- Discord token、guild ID、channel ID、role ID
- Notion token、Data Source ID
- 時區
- SLA / reminder 常數
- 課程或活動類型

目前 KariKari 是第一個租戶。程式還不是完整多租戶，但新的公司特定值應放在 `.env` / `TenantConfig`，不要寫死在功能邏輯中。

重要原則：

- 頻道名稱代表用途，ID 可替換。
- `SCHEDULE_CHANNEL_ID` 是「時間資訊彙整」用途，不是 KariKari 永久 ID。
- `TASK_CHANNEL_ID` 是「任務查詢入口」用途，不是 KariKari 永久 ID。
- 未來要平轉時，先改 `.env`，不要改核心流程。

## Discord Cogs

### `cogs/channel_guard.py`

共用頻道限制。

| 函式 | 用途 |
|---|---|
| `ensure_schedule_channel()` | 時間池 / 排課指令只允許在 `SCHEDULE_CHANNEL_ID` |
| `ensure_task_channel()` | 任務指令只允許在 `TASK_CHANNEL_ID` |

### `cogs/server_setup.py`

`/伺服器初始化` 以預覽／確認兩階段建立或對齊可平轉的角色與五用途頻道。未指派身分組的成員預設看不到營運區；完成後只回傳 `.env` 清單，不直接改本機秘密檔。

若對應 channel ID 留空，該類指令不限制頻道，方便新租戶初期測試。

### `cogs/scheduling_admin.py`

控方 / 管理者排程入口。

主要指令：

- `/時間資訊蒐集`
- `/從時間池建立任務`

主要責任：

- 建立月度時間收集公告。
- 讓控方選藝人比對共同空檔。
- 預覽與套用時間池整理。
- 舊 Schedule Request / Course Session 流程已從程式與設定完整移除；主流程只使用 Tasks。
- 月度時間池候選依課程／錄音／通告／其他分流；非課程先寫隱藏 Task 草稿。

### `cogs/scheduling_artist.py`

藝人時間填寫入口。

主要指令：

- `/管理可配合時間`
- `/我的時段`

主要責任：

- Discord UI 選週次 / 日期 / 時間模式。
- 寫入 `Availability Slots`。
- 送出時檢查正式任務衝突。
- 支援藝人刪除自己已填的時段。
- 藝人按「完成」後通知控方。

### `cogs/tasks_artist.py`

任務查詢入口。

主要指令：

- `/我的任務`
- `/任務總覽`

主要責任：

- 藝人查自己的 Discord 顯示任務。
- 控方查全部未結案任務。
- 任務分區顯示：逾期、需修改、三天內截止、進行中 / 待提交、待審核。

### `cogs/submissions.py`

任務 / 作業提交入口。

主要指令：

- `/提交任務`
- `/審核提交`

主要責任：

- 讀 Notion `Tasks.提交方式`，依控方指派方式引導提交。
- 支援 `檔案`、`Drive 連結`、`文字`、`混合`。
- Discord 檔案 `<=10MB` 時自動歸檔 Google Drive。
- `>10MB` 檔案不走 Discord，上雲端後貼連結。
- 寫 `Submissions & File Index`。
- 將 Task 轉為 `待審核` 並回寫 `最新提交 URL`。
- 審核最新版提交，回寫審核欄位，依結果更新 Task 並 DM 藝人。

完整產品規則見 [SUBMISSION_FLOW.md](SUBMISSION_FLOW.md)。

### `cogs/reminders.py`

背景提醒 loop。

主要責任：

- 月度時間池未填提醒。
- 月度時間池全員完成通知。
- Tasks 截止提醒與逾期升級。

提醒 loop 間隔由 `REMINDER_INTERVAL_MINUTES` 控制，目前預設 30 分鐘。

### `cogs/course_qa.py`

課程助教的 Discord 入口。

- `AI_CHANNEL_ID` 頻道支援自然語言，不需要 slash command 或 mention Bot。
- 每位藝人使用自己的私人 Thread，並以 Discord User ID 對應 Notion Artists。
- 新 Thread 首次先顯示課程附件索引；索引顯示後仍處理當次問題，後續可指定課程頁／檔案進行內容問答。
- 每次提問重新讀取 Notion；Notion 暫時失敗時才退回上次成功快取。
- 長回覆會切成 Discord 可接受的多則訊息。
- 公開問題刪除失敗只寫 log，不把權限錯誤洗到頻道。
- 以藝人 page ID 分開計算每日 token，並同時受租戶總額限制。
- 回答附問題回報入口；只把使用者主動填寫的回報寫入 Notion 布告欄。

### `cogs/operations_qa.py`

控方限定的 `/營運查詢`，只允許在營運內部頻道執行。讀取 Tasks、Projects、課程／會議摘要與 Submissions 審核回饋，不修改 Notion。

## Services

### `services/notion_api.py`

Notion API 薄封裝。

責任：

- Notion REST request。
- per-token client。
- 基本重試與節流。
- Data Source query / page create / update。

Notion 速率限制是重要約束，避免在 Discord 互動中大量同步 query。

### `services/repositories.py`

Notion 資料層與純邏輯集中處。

責任：

- 中文欄位名到 API payload 的唯一對應。
- dataclass models，例如 `Artist`、`AvailabilitySlot`、`TaskItem`、`ProjectItem`。
- Notion CRUD。
- 時段整理 / 任務分區 / 衝突判斷等可測邏輯。

如果 Notion 欄位改名，優先改這裡與 [NOTION_SCHEMA.md](NOTION_SCHEMA.md)。

### `services/time_collection.py`

月度時間收集與 Discord UI 輔助。

責任：

- 月份格式解析。
- 週次 / 日期分頁選項。
- 時間模式文案。
- 月度可配合時段文案。
- 月度收集進行中狀態與通知去重，持久化在 `.state/time_collection.json`。

### `services/overlap.py`

共同時段演算法。

責任：

- 多位藝人 availability interval 交集。
- 回傳符合最短時長的共同時間窗。

### `services/timeparse.py`

時間輸入解析與顯示。

責任：

- Discord modal 時間文字解析。
- 候選時段文字格式。
- 人類可讀時間顯示。

### `services/drive_archive.py`

Drive 歸檔路徑純邏輯。

責任：

- 從藝人專區 URL 解析 folder ID。
- 依任務類型決定資料夾。
- 回傳 `藝人專區 / 任務類型夾 / YYYY-MM` 路徑。
- 任務有指定 `Google Drive Folder` 時優先使用任務資料夾。

### `services/drive_api.py`

Google Drive API 操作。

責任：

- 讀 OAuth token。
- find-or-create 資料夾。
- 上傳 Discord attachment 到 Google Drive。
- 回傳 Drive File ID / URL。

### `services/course_intent.py` / `services/course_qa.py`

- `course_intent` 將自然語言問題整理成月份、課程、主題、逐字稿搜尋或附件名稱等檢索條件。
- `course_qa` 將頁面正文與使用者明確指定的附件分開組成 LLM context。
- LLM 只能根據已授權的課程紀錄回答，找不到內容時必須說明資料不足。
- 意圖判斷失敗採保守本地檢索；LLM 呼叫有固定 timeout、一次重試與完整紀錄邊界，避免模糊查詢與部分上下文造成誤答。

### `services/audit.py`

Audit Log 寫入。

責任：

- 寫入重要操作紀錄。
- 失敗不阻斷主流程。

### `services/teacher_confirm.py`

老師外部確認連結 token 層。

責任：

- HMAC-SHA256 簽名與驗證。
- 24 小時有效期。
- 成功確認後一次性使用，狀態寫入 `.state/teacher_confirm.json`。
- 依 `TenantConfig.teacher_link_secret` / `teacher_link_base_url` 判斷功能是否啟用。

## Web

### `web/server.py`

老師外部確認頁。

責任：

- `GET /confirm/{token}`：驗證 Task token、讀簽名候選時段、顯示 HTML 表單。
- `POST /confirm/{token}`：老師選時段或回覆都不行。
- 確認成功後通知控方，由控方在 Task 定案。

安全邊界：

- 本機只綁 `127.0.0.1`；對外由 Cloudflare Tunnel 或正式反向代理處理。
- token 只存 Task ID、候選顯示資料與過期時間，不包含 Notion token 或 Discord token。

## Discord 互動限制

Discord interaction 有約 3 秒回應限制。

原則：

- 可能超過 3 秒的 slash command 要先 `defer()`。
- Autocomplete 不能 defer，必須使用短 TTL 快取。
- UI callback 出錯時要回 ephemeral 錯誤，避免使用者只看到「互動失敗」。

## 平轉狀態

目前已做到：

- `TenantConfig` 集中租戶設定。
- Discord channel / role / guild ID 由 `.env` 控制。
- Notion Data Source ID 可由 `.env` 覆寫。
- 頻道用途與 ID 分離。

尚未完成：

- Notion 中文 property names 仍在 `repositories.py` 中硬對應。
- Discord 顯示文案仍偏 KariKari / 藝人語境。
- 還沒有完整 tenant registry；目前一次跑一個租戶。

下一個產品化方向：

- 抽出 Notion schema mapping。
- 抽出 customer terminology。
- 建立新租戶 onboarding checklist。
