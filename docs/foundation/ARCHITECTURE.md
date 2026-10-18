# 工程架構

這份文件給工程師與 AI 協作者理解程式切分方式。  
操作 SOP 看 [OPERATIONS_PLAYBOOK.md](../operations/OPERATIONS_PLAYBOOK.md)，Discord 指令看 [DISCORD_COMMANDS.md](../operations/DISCORD_COMMANDS.md)，Notion 欄位看 [NOTION_SCHEMA.md](NOTION_SCHEMA.md)。

## 系統定位

KariKari AI Ops Bot 是一個 Discord 操作入口，加上 Notion 營運資料層。

目前主流程：

`/時間資訊蒐集` → 藝人填月度可配合時段 → `/從時間池建立任務` → 控方選安排類型 → 不論課程或其他類型都建立 `Discord 顯示=false` 的隱藏 Task 草稿 → 控方填正式時間並勾選 `Discord 顯示=true` → Bot 自動通知

只想先看藝人共同空檔、還不確定要不要建任務時，可以用唯讀的 `/時間交集查詢`；它不寫入 Notion，看完可以直接接「建立任務」按鈕轉進 `/從時間池建立任務` 的流程。

課程正式時間如有異動，改為控方與老師人工協調後直接在 Task 更新開始／結束時間；沒有自動化的老師確認表單或對外連結。

**撞課情境的可見範圍分兩種**（`get_schedule_impact_context_for_artist` 的 `discord_visible_only`）：藝人端填時段只算已勾 `Discord 顯示` 的任務，未派發的草稿連任務名稱都不該讓藝人知道；控方端不帶旗標，比對共同空檔時仍把草稿算成已佔用，才不會把排掉的時段當空檔推出去。快取 key 含這個旗標，兩邊不會互相污染。

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
- `cogs.server_setup`

## 設定層

### `config.py`

`TenantConfig` 是一間公司 / 租戶的設定形狀。

包含：

- Discord token、guild ID、channel ID、role ID
- Notion token、Data Source ID
- 時區
- SLA / reminder 常數
- 小律查詢排除的會議類型（`qa_hidden_meeting_types`）

課程類型、任務類型不放在 `TenantConfig`：這兩個下拉選單改為執行時直接讀 Notion 對應 select 屬性的選項（`services/repositories.py` 的 `get_course_type_options()` / `get_task_type_options()`），避免 Notion 新增選項後還要改設定才會同步。

目前 KariKari 是第一個租戶。程式還不是完整多租戶，但新的公司特定值應放在 `.env` / `TenantConfig`，不要寫死在功能邏輯中。

重要原則：

- 頻道名稱代表用途，ID 可替換。
- `SCHEDULE_CHANNEL_ID` 是「時間管理」用途，不是 KariKari 永久 ID。
- `TASK_CHANNEL_ID` 是「任務查詢入口」用途，不是 KariKari 永久 ID。
- 未來要平轉時，先改 `.env`，不要改核心流程。

## Discord Cogs

### `cogs/channel_guard.py`

共用頻道限制。

| 函式 | 用途 |
|---|---|
| `ensure_schedule_channel()` | 時間池 / 排課指令只允許在 `SCHEDULE_CHANNEL_ID` |
| `ensure_task_channel()` | 任務指令只允許在 `TASK_CHANNEL_ID` |

### `cogs/ui_text.py`、`cogs/paging.py`

共用的訊息長度處理（Discord 單則訊息 2000 字上限）。

| 函式 / 類別 | 用途 |
|---|---|
| `ui_text.trim_message()` | 最後一道保險：超長時在整行邊界截斷，不會把一行切一半 |
| `ui_text.fit_items()` | 有下拉選單可看完整資料時，文字預覽依實際長度收斂並回報剩餘筆數 |
| `ui_text.paginate_blocks()` | 把「不可切開的區塊」（例如同一天的候選時段）分頁 |
| `ui_text.paginate_sections()` | 把「分區標題＋內容」分頁；跨頁會重印標題並標示「（續）」 |
| `paging.PagedTextView` | 共用翻頁 View（上一頁／下一頁），只有一頁時按鈕停用 |

原則：清單型畫面**寧可分頁也不要截斷**。只有在下拉選單本來就能看到完整資料時，才用
`fit_items` 做預覽收斂，並誠實顯示還有幾筆。

### `cogs/server_setup.py`

`/伺服器初始化` 以預覽／確認兩階段建立或對齊可平轉的角色與六用途頻道。未指派身分組的成員預設看不到營運區；完成後只回傳 `.env` 清單，不直接改本機秘密檔。

若對應 channel ID 留空，該類指令不限制頻道，方便新租戶初期測試。

### `cogs/scheduling_admin.py`

控方 / 管理者排程入口。

主要指令：

- `/時間資訊蒐集`
- `/從時間池建立任務`
- `/時間交集查詢`（唯讀，只看共同空檔不寫入 Notion；查完可接「建立任務」按鈕轉進 `/從時間池建立任務`）

主要責任：

- 建立月度時間收集公告。
- 讓控方選藝人比對共同空檔。
- 預覽與套用時間池整理。
- 舊 Schedule Request / Course Session 流程已從程式與設定完整移除；主流程只使用 Tasks。
- 月度時間池候選依課程／錄音／通告／其他分流；一律先寫 `Discord 顯示=false` 的隱藏 Task 草稿。

### `cogs/scheduling_artist.py`

藝人時間填寫入口。

主要指令：

- `/管理可配合時間`

主要責任：

- 跨月份總覽，以及指定月份的刪除、新增與補填。
- Discord UI 選日期 / 時間模式。
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

- `/我的任務` 內的「提交任務」操作
- `/審核提交`

主要責任：

- 交件只有一種：選任務後直接進入公司 Drive 上傳流程（`start_file_upload_wait(..., drive_only=True)`）。`Tasks.提交方式` 2026-10-11 起不再讀取，原本的分流函式與 `SubmissionModal` 已移除。
- 藝人檔案統一上傳到 Bot 建立的公司 Google Drive 指定資料夾；不接收 Discord 作業附件，也不提供私人 Drive 連結作為檔案提交 fallback。
- 老師批改檔仍使用獨立 pending attachment／Drive 回饋流程。
- 寫 `Submissions & File Index`。
- 將 Task 轉為 `待審核` 並回寫 `最新提交 URL`。
- 審核最新版提交，回寫審核欄位，依結果更新 Task 並 DM 藝人。

完整產品規則見 [SUBMISSION_FLOW.md](../operations/SUBMISSION_FLOW.md)。

### `cogs/reminders.py`

背景提醒 loop。

主要責任：

- 月度時間池未填提醒、全員完成通知、排課 SLA。
- 任務派發、課後作業通知、未確認重送（藝人＋`相關同仁`）。
- 批次重送：撈 Tasks `重送通知`=true，先取消勾選再重送（`_check_batch_resend`）。
- 每日催辦（`_check_daily_nags`，`DAILY_NAG_HOUR`）：已逾期／需修改催藝人、待審核催審核人員。
- 課後提醒老師（`_check_teacher_post_class`），同一堂課合併一則。
- 修改期限逾時通知控方（`_check_revision_deadlines`）。
- Tasks 截止前提醒與逾期升級（`_check_task_reminders`）。
- 發文待確認發布每週彙整（`_check_publish_confirm_digest`）。

提醒 loop 間隔由 `REMINDER_INTERVAL_MINUTES` 控制，目前預設 30 分鐘。

**去重狀態不放記憶體**：截止前提醒、每日催辦日期、課後提醒次數、任務通知的訊息編號都寫在 `.state/task_notification_log.json`（`services/notification_log.py`，同一 process 共用一份快取，避免 loop 與按鈕 callback 互相覆蓋）。逾期升級只靠 Notion 狀態 `已逾期` 去重。

### `cogs/course_qa.py`

小律（會議秘書）的 Discord 入口。

- `AI_CHANNEL_ID` 頻道支援自然語言，不需要 slash command 或 mention Bot。
- 每位藝人使用自己的私人 Thread，並以 Discord User ID 對應 Notion Artists。
- 查詢範圍是已標記藝人可見、且 `會議類型` 不在 `qa_hidden_meeting_types` 排除清單內的會議紀錄，不限於課程。
- 新 Thread 首次先顯示附件索引；索引顯示後仍處理當次問題，後續可指定頁面／檔案進行內容問答。
- **藝人端不輸出 Notion 網址**（全系統規則，見 `services/notion_links.py`）：附件索引、月份總覽與回答來源都只寫「日期｜頁面名稱」。網址也不進 LLM context（否則模型會抄進回答），回答送出前再過濾一次 Notion 網域；會議內容裡的非 Notion 連結照常保留。
- 每次提問重新讀取 Notion；Notion 暫時失敗時才退回上次成功快取。
- 長回覆會切成 Discord 可接受的多則訊息。
- 公開問題刪除失敗只寫 log，不把權限錯誤洗到頻道。
- 以藝人 page ID 分開記錄 token，並保留個人與租戶兩層可設定護欄；目前兩者皆為 `0`（不限額）。
- 使用 OpenAI `gpt-5.6-luna` 的 `/v1/chat/completions`。LongCat 明確禁止使用；URL 或模型名稱命中 LongCat 時，系統會在發出請求前拒絕。回應解析相容文字、content list、`choices[].text` 與 `output_text`。
- HTTP 200 但正式內容空白或格式不完整時視為失敗並重試，不會把 reasoning content 當成藝人回答。
- 回答附問題回報入口；只把使用者主動填寫的回報寫入 Notion 布告欄。

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
- 回傳 `藝人專區 / 專案名稱 / 任務夾` 路徑（2026-10-13 改版，規則 M4-07）；專案名稱由呼叫端用 `repositories.task_project_name()` 取得，沒掛專案 → `未分專案`。`DEFAULT_TYPE_FOLDERS` 已移除。
- 任務已回寫 `Google Drive Folder` 時，提交與老師回饋都直接用那個夾，不受專案改名影響。
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

### `services/notification_log.py`

任務通知的持久化紀錄（M3-23）：已送出的提醒階段（key＝任務｜階段｜期限日）、每則帶按鈕通知的訊息編號（按「收到任務」時編輯成已收到）、每日催辦最後日期、課後提醒次數。保留 45 天。

### `services/daily_nag.py` / `services/teacher_reminder.py`

每日催辦與課後提醒老師的純邏輯（分組、到期判斷、訊息文字），cog 只負責發送與寫狀態。

### `services/monthly_board.py`

公告頻道的月度排程（M6-02）：篩選當月已派發的公開類型任務、組訊息、算內容指紋。由 `cogs/bulletin.py` 的 `sync_monthly_board()` 隨布告欄同步呼叫，內容有變才原地編輯同一則；狀態在 `.state/monthly_board.json`。

### `services/task_notifications.py`

所有任務類私訊的文字（派發、課後作業、截止前提醒、同仁知會、發文待確認彙整）。藝人端一律不含 Notion 網址：任務說明經 `_artist_facing_description()` 清掉內部欄位與 Notion 連結。

### `services/scheduling_sla.py`

排課 SLA 狀態層（`TaskWorkflow` / `CollectionWorkflow`）。

責任：

- 追蹤時間收集與任務建立的進度時間戳，供 `reminders.py` 判斷是否要提醒控方。
- 舊老師確認相關欄位已從 dataclass 移除；讀舊 `.state/scheduling_sla.json` 時會忽略已不存在的欄位，不會因為 schema 變動而炸掉。

## Web

### `web/server.py`

只暴露 Discord OAuth callback，供 `/伺服器初始化` 同步各頻道 slash command 可見範圍時接住 Discord 的授權回呼。

責任：

- `GET /discord/oauth/callback`：接收 Discord OAuth 授權碼並完成初始化流程。

安全邊界：

- 只需要 `127.0.0.1` 本機可達，不對外開放，不需要對外網域或 Tunnel。
- 未設定 `DISCORD_CLIENT_SECRET` 時整個服務不啟動。
- 老師外部確認頁（`/confirm/{token}`）與相關 token 簽名機制已移除，不再有對外暴露的 HTTP 表單。

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
