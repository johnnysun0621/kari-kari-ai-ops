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
| [SUBMISSION_FLOW.md](docs/operations/SUBMISSION_FLOW.md) | 任務提交方式、Google Drive 資料夾、審核通知 |
| [DRIVE_OAUTH_SETUP.md](docs/operations/DRIVE_OAUTH_SETUP.md) | Google Drive 歸檔 OAuth 設定教學（含平轉重跑流程） |

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
- **逾期提醒不看提醒規則一律觸發**（含規則空白的任務）；截止前提醒才照規則
- **提醒規則空白＝套用 `default_reminder_rules`（2026-09-20 起預設「當天」）**，不是不提醒；
  一旦勾了任何規則就完全以勾選為準，預設不疊加（否則控方無法把提醒收斂成只剩當天）。
  「5 天前」已從 Notion 選項與預設移除——提前幾天交件改由「提前交件」決定，不再靠提醒規則塞
- **「逾期」不在 `REMINDER_RULE_DAYS` 裡，但不是無用選項**：只勾它＝規則非空、擋掉預設，
  等於「事前別提醒，逾期再講」。這是控方關閉事前提醒的唯一方式
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
  任務逾期用 Notion 狀態「已逾期」持久化（重啟不重複轟炸）

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
藝人檔案提交統一走 Bot 建立的公司 Google Drive 指定資料夾；Discord 不接收藝人作業附件，也不提供私人 Drive 連結替代入口。入口 3 小時有效。
老師外部確認連結屬歷史相容功能，目前不啟動；主流程是 `/從時間池建立任務` 後由控方在 Tasks 完成正式安排。
