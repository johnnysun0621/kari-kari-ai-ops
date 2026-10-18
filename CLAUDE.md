# CLAUDE.md — AI 協作接手指南

Discord 排課／營運 bot（Python + discord.py + Notion API）。KariKari 是第一個租戶，
最終要平展化成可賣給多間公司的產品。**你的任務通常是：接續 ROADMAP 的下一個 Phase。**

## 文件地圖（先讀這些再動手）

**根目錄**（入口，很少變動）：

| 文件 | 內容 |
|---|---|
| [README.md](README.md) | 專案入口、安裝、.env 設定、常用文件 |

**`docs/foundation/`**（架構基礎，很少變動）：

| 文件 | 內容 |
|---|---|
| [HANDOFF.md](docs/foundation/HANDOFF.md) | AI／工程師協作者接手規則、改檔對照、測試要求 |
| [ARCHITECTURE.md](docs/foundation/ARCHITECTURE.md) | 程式模組責任、資料邊界、平轉狀態 |
| [PROJECT_PRINCIPLES.md](docs/foundation/PROJECT_PRINCIPLES.md) | 產品原則（多租戶平展化、資料安全）——Johnny 親寫，架構決策的最高依據 |
| [NOTION_SCHEMA.md](docs/foundation/NOTION_SCHEMA.md) | Notion 資料庫交互邏輯 |
| [FLOWCHART.md](docs/foundation/FLOWCHART.md) | **系統架構＋7 張流程圖**：架構分層、任務一生、死線判定、月度排課、藝人端資料邊界、背景自動化、資料庫責任分界 |

**`docs/sop/`**（給控方與藝人的操作手冊，改行為要同步更新）：

| 文件 | 內容 |
|---|---|
| [ADMIN_SOP.md](docs/sop/ADMIN_SOP.md) | 控方操作順序＋Notion 任務欄位對照表 |
| [ARTIST_SOP.md](docs/sop/ARTIST_SOP.md) | 藝人四件事：回報時間、確認任務、交作業、問小律 |

**`docs/operations/`**（描述系統目前實際行為，改功能要同步更新）：

| 文件 | 內容 |
|---|---|
| [TESTING.md](docs/operations/TESTING.md) | 本機測試、Discord 手動驗收、Notion 驗收 |
| [SUBMISSION_FLOW.md](docs/operations/SUBMISSION_FLOW.md) | 任務提交（一律上傳公司 Drive）、Google Drive 資料夾、審核通知 |
| [DRIVE_OAUTH_SETUP.md](docs/operations/DRIVE_OAUTH_SETUP.md) | Google Drive 歸檔認證設定教學——服務帳號（推薦）與 OAuth（舊方式）兩條路線，含平轉重跑流程 |

**`docs/history/`**（決策當下的規劃紀錄，凍結不回頭改）：

| 文件 | 內容 |
|---|---|
| [DELIVERY_PLAN.md](docs/history/DELIVERY_PLAN.md) | **v1.0 交付衝刺執行中（D0-D8）——接手先做這裡的下一個未勾項** |
| [ROADMAP.md](docs/history/ROADMAP.md) | **進度總表＋下一步**＋進度日誌＋待 Johnny 提供事項——接手先看這裡 |
| [RESEARCH.md](docs/history/RESEARCH.md) | 需求研究：Kickoff 時期資料庫盤點、Gap 分析、決策收斂紀錄 |

## 接手流程

1. 看 ROADMAP「Phase 進度總表」找下一個 🔜/⬜ 項目（含實作要點）
2. 依下方鐵則實作；重用 services/ 既有 helper（先 grep 再寫新的）
3. 驗證：`.venv/bin/python -m compileall bot.py cogs services tests config.py web` ＋ `.venv/bin/python -m pytest tests/ -q`
4. Commit（訊息含 Phase 編號），並更新 ROADMAP 的進度總表與進度日誌

## 開發指令

```bash
source .venv/bin/activate
python -m compileall bot.py cogs services tests config.py web
python -m pytest tests/ -q
python bot.py                      # 需 .env（見 README）
```

## 架構速覽

- [config.py](config.py)：`TenantConfig` dataclass ＝ 一間公司的全部設定；`get_tenant()` 取得。
  模組常數是向後相容別名，**新程式碼一律用 `get_tenant()`**
- [services/notion_api.py](services/notion_api.py)：Notion 薄封裝；**per-token 連線＋節流（~3 req/s）**
- [services/permissions.py](services/permissions.py)：身分／權限判斷唯一入口（Phase 3 權限層起點）
- [services/repositories.py](services/repositories.py)：**中文欄位名 → API payload 的唯一對應處**；
  Notion 改欄位名只改這裡。含 dataclass 模型、純函式（分區/去重/提醒階段）
- [cogs/](cogs/)：channel_guard（頻道分流＋公告頻道 fallback）、scheduling_admin（排課管理）、
  scheduling_artist（藝人填時段）、tasks_artist（任務查詢）、submissions（作業提交與審核通知）、reminders（背景提醒 loop，30 分鐘一輪）
- 測試全是純函式測試（不打網路）；async 用 `asyncio.run` ＋ monkeypatch `notion_api.query_data_source`

## 鐵則（違反＝review 打回）

1. 公司特定值（DS ID、頻道、用語、SLA）從 `get_tenant()` 注入，不硬編碼
2. 不新增全域單例；快取／節流／連線以 token 或租戶資料源為 key
3. Notion 查詢一律帶 filter（關聯／狀態／`_future_date_filter`），**不撈全表再過濾**
4. 破壞性操作先預覽；封存（archive）不刪除；重要操作寫 `audit.log_event`
5. 互動處理第一行先 `defer()` 或立即回應（Discord 3 秒硬限）；autocomplete 必須走快取
6. 每個功能配純函式測試；新增測試工廠前先 grep `tests/`（曾發生同名 `task()` 遮蔽既有工廠）
7. **藝人端零 Notion 入口**（Johnny 2026-09-18 拍板）：任何藝人看得到的輸出都不得帶 Notion 網址——
   私訊、公告區、課程助教、link button 全部算。原因是 Notion 的逐頁權限管不到頁面上方的導覽列，
   藝人拿到任一頁網址就能往上翻到整個資料庫（一筆任務 → 全部藝人的 Tasks）。
   網址也不能進 LLM context，模型會照抄。控方／老師頻道不受限。
   工具：`services/notion_links.py`；守門測試：`tests/test_artist_no_notion_links.py`
8. **模組化／積木化**（Johnny 明確要求）：共用邏輯放 services/（permissions、repositories…）、
   共用 UI helper 放獨立模組（channel_guard、ui_text 模式）；**cog 之間不 import 彼此的私有函式**；
   helper 重複第二次就抽出來

## 刻意設計——看起來像 bug 但不是（別「修」它們）

- **時段去重是性質無關、較寬窗勝出**：`_slot_nature_priority` 恆回 1 是故意的；
  測試 `test_long_legacy_slot_replaces_contained_legacy_slot_without_priority` 明文保護
- **任務的分類只看 `關聯專案` 的 `專案類型`，不看 `任務類型`**（Johnny 2026-10-03 拍板）：
  `任務類型` 管公告區（`PUBLIC_TASK_TYPES`）與硬擋時段（`HARD_BLOCK_TASK_TYPES`），
  兩套各管各的。分類走 `TaskItem.project_types`，值由 Notion 的 rollup
  （`專案類型`，`show_unique`，經 `關聯專案`）帶進來——所以 bot 不必額外查 Projects。
  判斷用 `task_has_project_type()`（對多值正確）；`task_project_type()` 在多值時回 None，
  因為硬選一個會讓行為取決於 Notion 的回傳順序。
  **`月度排程 （發文）`／`（行程）` 是半角空格 ＋ 全角括號**，客戶手打的；一律引用
  `PROJECT_TYPE_*` 常數，別重打字串。rollup 被改名或改壞時 `project_types` 回空
  ＝「沒有分類」，不炸流程——代價是靠分類的功能會靜默失效，所以
  `tests/test_notion_option_values.py` 那份選項清單要在 Notion 改動後人工同步
- **逾期提醒不看提醒規則一律觸發**（含規則空白的任務）；截止前提醒才照規則
- **課程培訓類任務完全不進逾期流程**（Johnny 2026-10-03 拍板，規則 M5-03）：
  課程同時有「上課時間」與「課後交付」兩個期限，混著判會在上課日隔天把所有課程
  任務標紅——實際上只是老師還沒發東西。一片紅的看板等於沒有看板。
  依據是 **Projects 的 `專案類型`＝`課程培訓`**，不是 Tasks 的 `任務類型`＝
  `課程時間&作業繳交`（兩個名字很像，寫錯就豁免不到）。
  代價：課程任務沒有逾期警訊，完全靠 M5-01 的催老師機制。
  **豁免必須放在 `compute_due_reminder_stages` 的 `task_deadline_passed` 分支裡
  直接 return `[]`**，不能讓它往下走規則分支——期限已過時 `days_left` 是負的，
  `days_left <= days` 對每一條規則都成立，等於把 D-5／D-3／當天一次全部補發。
  也不能把豁免塞進 `task_deadline_passed()` 本身，同一個理由。
  控方在 Notion 手動設的「已逾期」仍照顯示——系統不自己判，但不蓋掉人的判斷
- **`default_reminder_rules` 與任務勾選的規則是聯集，不是替換**（Johnny 2026-10-03 拍板）：
  預設永遠生效，勾選只能往上加。所以每個任務至少都會收到預設的那幾次提醒，
  **控方無法用留空或改勾選來減少**——要減少只能改租戶預設值。這是刻意的取捨。
  預設＝`5 天前,3 天前`（09/22 會議「D-5、D-3 兩個節點並行」）。
  改預設要同時改 `.env`、`.env.example`、`config.py` fallback 與
  `test_tenant_default_is_d5_and_d3`，四處一致
- **提醒天數從「追蹤期限」往前算，不是從截止日**：`task_action_deadline()` 在勾了
  「提前交件」時回交件日，所以 D-5 是交件日前 5 天。語意是「進入窗口就觸發一次」，
  不是「正好那天才觸發」（`days_left <= days`，bot 停機補跑仍會發）
- **「逾期」不在 `REMINDER_RULE_DAYS` 裡，且改成疊加後已無法用來關閉事前提醒**：
  以前只勾它＝規則非空、擋掉預設，等於「事前別提醒，逾期再講」；現在預設照樣生效，
  這個手法失效。保留它是為了語意完整（`test_overdue_only_rule_no_longer_opts_out` 釘住）
- **「提前交件」☑ 會把追蹤的期限換成交件死線**（`approval_submission_deadline`）：`提交期限` 有填用它，
  沒填＝`截止日 − approval_lead_days`。逾期以交件日判定，藝人看到「交件 9/25」不是「截止 9/30」。
  一個任務進行到底、不拆兩筆，所以發布日當天沒有提醒——這是客戶決議的取捨，不是漏做
- **「待審核」不發截止前提醒**（`compute_due_reminder_stages`）：東西交出去了球在公司，
  再催藝人會被當成 bug。逾期判定同樣停在待審核
- **date-only 截止日當天整天有效，隔天才算逾期**（`task_deadline_passed`）
- 任務「待審核」優先於「逾期」分區（已交出＝球在公司，不對藝人顯示逾期）
- **「提醒確認(From 藝人)」欄位是 text 不是 checkbox，且 Bot 送出提醒時自己先寫一行「已發送給 N 人」**：
  多藝人任務要逐人逐階段記，checkbox 記不了誰按了；送出行讓控方分得出「還沒發」和「發了沒人按」。
  同一行重複不再寫（`merge_reminder_ack_line`）；欄位不存在時降級只留 Audit Log，不炸流程
- 提醒去重：排課類在記憶體 `_sent`（重啟最多重發一次，可接受）；
  任務逾期**只**用 Notion 狀態「已逾期」去重——控方改回進行中但沒延期，下一輪就再通知一次（M2-13），
  不要再加記憶體去重，否則行為又會取決於 bot 有沒有重啟過；
  截止前提醒、每日催辦日期、課後提醒次數、通知訊息編號寫在 `.state/task_notification_log.json`
  （`services/notification_log.py`，一律經 `shared()`／`persist()` 存取，loop 與按鈕 callback 各自 load/save 會互蓋）。
  第一次啟用持久化時，窗口內的階段只記錄不補發（`reminders_baseline_done`），否則一上線又整批重發
- **每日催辦（M3-11）取代了「待審核每 12 小時」與「修改期限 24 小時前」**：先寫 `daily_nag_last_date` 再發送，
  中途失敗也不會同一天每 30 分鐘重試轟炸。`REVIEW_REMINDER_INTERVAL_HOURS` 已不生效
- **任務相關對象採雙 Relation（M7-06 定案，2026-10-13）**：`關聯藝人` → Artists（執行者，收按鈕與狀態流程）、`相關同仁` → People（純知會）。
  不建立 `相關人士`：一個 relation 只能指向一個資料庫，改名解決不了同仁掛不上去。同仁訊息沿用藝人版內容（同樣不帶 Notion 網址）、沒有按鈕；
  有藝人時派發是否完成只看藝人，否則同仁私訊失敗會讓整筆重試、藝人重複收到
- **分支 `feat/m7-06-rename-related-people` 已棄用，絕不可合併部署**：它把查詢鍵改成 `相關人士`，
  正式資料源沒有這個欄位，`_read_relation_ids` 讀不到回空 → 藝人端全空、提醒全停，而且不報錯
- **課後提醒老師依「課名＋結束時間＋負責老師」合併**：Notion 上同一堂課是每位學生各一筆任務

## Notion 資料層

- Runtime 目前讀寫 9 個 Notion Data Sources；**ID 全部必填於 `.env`，程式碼不留 fallback**
  （本 repo 是 public；且 .env 漏載時連到錯誤資料庫比連不上更難發現）。
  缺任一組由 `bot.py` 啟動時 `config.missing_data_source_env()` 擋下；
  守門測試 `tests/test_no_hardcoded_data_source_ids.py`。
  其他 workspace 資料庫與 views 不代表 Bot 會讀取
- 速率 ~3 req/s per integration token——這是產品天花板，效能問題先想快取，不是拆節流
- 欄位級 schema 與功能對應：見 RESEARCH.md 第二節
- Audit Log 寫入失敗不阻斷主流程（audit.py 已包好）
- 月度收集進行中狀態持久化在 `.state/time_collection.json`（bot 本機狀態，非營運資料；已 gitignore）

## 目前狀態（2026-08-08）

月度時間池主流程、任務查詢、任務提醒、雙頻道分流、Drive 檔案歸檔、`/我的任務` 內建提交入口、`/審核提交` 已落地。  
藝人檔案提交的**最終存放處**統一是 Bot 建立的公司 Google Drive 資料夾，藝人不決定存放位置。
藝人選任務後一律拿到 Bot 建立的版本資料夾自行上傳（`start_file_upload_wait(..., drive_only=True)`，入口 3 小時有效）；
**藝人的 Discord 附件一律拒收**並引導回資料夾。Discord 附件代傳只保留給老師批改檔流程。
Drive 路徑：`<藝人名>/<類型夾>/<YYYY-MM>/<YYYY.MM.DD_任務名>/<任務名_日期_ver.N>`
（**2026-10-13 改為 `<藝人名>/<專案名稱>/<YYYY.MM.DD_任務名>/<任務名_日期_ver.N>`，沒掛專案進 `未分專案`**——
類型夾看任務類型，而 173 筆任務有 107 筆沒填，幾乎全進「其他」；專案名稱以名稱 find-or-create，改名會產生新夾，
所以已回寫 `Google Drive Folder` 的任務一律直接用那個夾；讀不到專案時寧可不建資料夾，也不要落到「未分專案」放錯）；
任務自填 `Google Drive Folder` 時直接用那個夾。每次提交建新的 ver.N，退件重繳留完整歷史。
**`提交方式` 欄位已全部停用**（Johnny 2026-10-03 決議，2026-10-11 拆除）：只留「上傳檔案到公司 Drive」一種，
不再有「貼雲端連結」與「純文字」——藝人貼自己的連結時檔案不會進公司 Drive，與上面的原則衝突。
程式端的分流函式、`SubmissionModal`、`TaskItem.submit_method` 都已移除，Notion 的 `提交方式` 欄位也已刪除（2026-10-13 確認）。
老師外部確認連結屬歷史相容功能，目前不啟動；主流程是 `/從時間池建立任務` 後由控方在 Tasks 完成正式安排。
