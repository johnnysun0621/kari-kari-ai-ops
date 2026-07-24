# KariKari AI Ops｜完全體開發路線圖與進度

> 目標：把系統開發到「完全體」＝可販售的 showcase。KariKari 是第一個落地站，
> 架構全程遵守 [PROJECT_PRINCIPLES.md](PROJECT_PRINCIPLES.md)（多租戶平展化：架構不擋路）。
> 需求源：2026-06-29 Kickoff 會議——完整研究與 Gap 分析見 [RESEARCH.md](RESEARCH.md)。
> AI 協作者接手流程見 [CLAUDE.md](CLAUDE.md)。

## 驗收標準（Kickoff MVP Definition of Done）

藝人能在 Discord 查到自己的任務、完成作業／企劃提交、填寫可用時間；
管理者能在 2 天內完成排課、發布正式通告；所有提交與決策可回溯（Audit Log）。

## 已收斂決策（2026-07-08）

1. **商業形態**：先做 KariKari 完全體當 showcase；多租戶只做「架構不擋路」
2. **完全體範圍**：P0 全六項 ＋ AI 問答（架構預留、LLM 先不接）＋ 合約履約提醒 ＋ 財務審批
3. **課程安排**：課程、老師、正式時間與作業集中在 Tasks；不維護獨立課程場次資料庫
4. **部署**：開發期續用錄音室 Mac；程式保持「搬雲端只改設定」；對外連結用 Cloudflare Tunnel
5. **順序**：P0 補齊（→7 月底）→ AI-ready（8 月底）→ 合約 → 財務
6. **財務核決**：先按單一核決人設計、欄位預留多人；實作前再與公司確認
7. **歸檔結構（2026-07-10）**：藝人專區→任務類型夾→YYYY-MM（取代 Kickoff 課程→日期→藝人；隱私隔離優先）；任務指定資料夾可覆蓋

## 架構原則（每個 Phase 都適用）

- 新程式碼一律吃 `config.get_tenant()` 設定物件，不吃模組常數
- 不新增全域單例；連線／節流／快取以 token 或租戶為 key
- 權限是底層：先判身分（Artists／Members），再查資料
- 破壞性操作先預覽、封存不刪除、全部寫 Audit Log
- 效能紀律：短 TTL 快取、節流、未來界限查詢、Notion 端過濾不撈全表
- 每個功能：pytest 純函式測試 ＋ 真機 Discord 驗收

## Phase 進度總表

| Phase | 內容 | 狀態 | Commit |
|---|---|---|---|
| 0 | 地基：TenantConfig、per-token 連線/節流、TOCTOU 修正 | ✅ 完成 | `ef64c0f` |
| 1a | 任務查詢（P0-2）：/我的任務、/任務總覽 | ✅ 完成 | `ec0a1c8` |
| 1d | 任務提醒（P0-4）：提醒規則＋逾期升級 | ✅ 完成 | `81eb307` |
| 1b | 作業提交（P0-3）：/提交任務（連結/文字/檔案歸檔 全通） | ✅ 完成 | `47b55b4` / `0950222` |
| 1c | 審核流：/審核提交 通過/需修改/退回＋DM 藝人＋批改檔回傳 | ✅ 完成 | `0950222` / D1 |
| 1e | 頻道/身分組初始化（P0-①）：/伺服器初始化 或 SOP | ⬜ 待做 | — |
| 2 | 老師外部確認連結（歷史相容模組） | ⏸️ 不列入目前主流程 | `5a938a7` |
| 3 | AI-ready：permissions 權限層、/查詢 結構化查詢、LLM adapter 預留 | ⬜ 8 月底 | — |
| 4 | 合約履約提醒（新 Notion DB＋90/30 天預警＋補救任務） | ⬜ 9 月 | — |
| 5 | 財務審批（新 Notion DB＋申請→核決→執行→結案） | ⬜ 9-10 月 | — |
| 6 | 商品化收尾：遷雲端 VPS、tenant onboarding 包 | ⬜ 10 月 | — |

## 課程助教目前進度（2026-07-24）

- ✅ 課程助教頻道自然對話：不需要 slash command，也不需要 `@Bot`。
- ✅ 依 Discord User ID 對應藝人，只讀取該藝人可見的課程與會議記錄。
- ✅ 每位藝人建立自己的私人 Thread，避免課程內容在公開頻道曝光。
- ✅ 啟動時預熱課程目錄；每次提問重新讀 Notion，失敗才使用快取。
- ✅ 先顯示課程附件索引，再由藝人指定課程頁或檔案後提問。
- ✅ PDF、DOCX、PPTX、XLSX 與常見文字檔文字抽取；附件正文分離，避免未指定檔案影響回答。
- ✅ 移除 15 秒提問冷卻；長回覆自動分段。
- ✅ 公開問題刪除失敗改為只記 log，不在頻道顯示權限警告。
- ✅ 相關測試目前 213 passed；完整指令見 [DISCORD_COMMANDS.md](DISCORD_COMMANDS.md)。

目前限制：掃描圖片 PDF、舊版 `.doc`／`.ppt`、加密檔案尚未支援；刪除公開問題仍需要 Bot 的 `管理訊息` 權限，但不影響私人 Thread 問答。

## 進度日誌

### 2026-07-22（Tasks 單一安排來源）

- 課程安排改由 `/時間池比對` 選課程類別、課程培訓 Project、老師與課程名稱後，直接建立 Tasks 的「課程與作業」。
- 錄音／通告／其他只提供共同空檔候選，由控方手動在 Notion 建立 Project / Task。
- `Schedule Requests`、`Course Sessions` 已移至 Notion 垃圾桶；Tasks 集中管理課程、老師、正式時間、作業與審核。
- 老師確認表單已重新接回課程 Task 流程；舊 `/排課*` 仍不列入目前主流程。

### 2026-07-18（v1.0 D1 審核體驗補強）

- `/審核提交` 選到提交後，直接顯示 Drive 連結、文字摘要、Notion 提交頁與關聯任務連結，老師不用先跳 Notion 才知道要審什麼
- 審核送出後新增 `附批改檔` 入口；控方可在同一頻道上傳 10MB 以下批改檔
- 批改檔自動歸檔到 `藝人專區/老師回饋/YYYY-MM/`，Drive 連結併入 Submissions `審核回饋`，並補發 DM 給藝人
- 文件同步：DISCORD_COMMANDS、SUBMISSION_FLOW、OPERATIONS_PLAYBOOK、DELIVERY_PLAN
- 驗證：submissions / Drive archive 相關測試先行通過；全量驗收以 D1 commit 前結果為準

### 2026-07-10 深夜（歷史：Phase 2 老師外部確認連結開發版）

- 新增 `TEACHER_LINK_SECRET` / `TEACHER_LINK_BASE_URL` / `WEB_PORT` 設定；未設定 secret 時功能停用，既有人工 `/排課確認` 流程不受影響
- 新增 aiohttp web 服務：`/confirm/{token}` 顯示候選時段，老師可免 Discord 選時段或回覆都不行
- 新增簽名 token：HMAC-SHA256、24 小時有效、成功確認後一次性失效，狀態存 `.state/teacher_confirm.json`
- `/排課比對` 選定候選後，若已設定對外網址，會產生老師確認連結；老師確認後共用同一條 `scheduling_flow` 建 Course Session、更新 Schedule Request、發正式通告與 DM
- 新增 `scripts/start_tunnel.sh`：開發期可用 Cloudflare quick tunnel，把外部網址填入 `.env` 後重啟 bot
- 測試：`168 passed`，包含 token 簽名/過期/一次性、HTML escape、既有 command permission 回歸

### 2026-07-10 晚（提交 UX＋審核通知＝Phase 1b/1c 完全體）

- services/drive_api.py：find-or-create 資料夾＋上傳（asyncio.to_thread 不卡 event loop、
  連線以 token 為 key）；Submissions 欄位擴充（File ID／原始檔名／大小 MB）
- `/提交任務`：第一關先顯示可提交任務、提交方式與老師要求摘要；選任務後若 Notion 指定 `檔案`，Bot 在同頻道等待附件，不要求藝人跳私訊或重打；
  入口 3 小時有效，10MB 以下自動歸檔 `專區/類型夾/YYYY-MM/`；>10MB 擋下引導貼連結；上傳失敗不寫半套資料
- `/審核提交`：管理端審核最新版提交，支援 `通過` / `需修改` / `退回`＋回饋；
  同步更新 Tasks 並 DM 藝人；Notion 手動審核也可由背景 loop 通知
- 真機驗證：Johnny 專區實際上傳成功（資料夾自動建立、測試檔已清）
- MVP 三動詞（查任務／提交／填時間）全部完成

### 2026-07-10（Drive 整合前置全通）

- 還原 6/29 smoke test 的 OAuth 設定（~/Documents/Kali Kali AI Ops），重用其用戶端
- `ee8e182` Drive OAuth 套件：DRIVE_OAUTH_SETUP.md 教學（重用/從零兩路線）、
  authorize_drive.py／check_drive.py 腳本、TenantConfig.drive_root_folder_id
- 完成授權（完整 drive scope，取代舊 drive.file）；驗證總資料夾＋2 個藝人專區；
  專區連結回填 Notion Artists ✅
- 歸檔結構拍板：藝人專區→任務類型夾→YYYY-MM（`09f0ae9` 路徑積木＋11 測試）
- 下一步：Drive 上傳層與提交 UX（已完成於 2026-07-10 晚）

### 2026-07-09 深夜（Phase 1b 作業提交）

- `/提交任務`：藝人選開放提交的任務 → 表單貼雲端連結或填文字（至少一項）→
  寫入 Submissions（來源 Discord、權限=藝人本人、審核狀態=待審核、檔案類型自動對應）
- 重交支援：同任務舊提交自動取消「是否最新版」；任務轉待審核＋回寫最新提交 URL
- 通知：營運內部頻道收「新提交待審核」（含連結與 Notion 頁）；Audit 全記錄
- 檔案上傳未開通（等 Drive OAuth 憑證）；「檔案」類任務引導改貼雲端連結
- 頻道接線：公告區／營運內部專用 上線（.env 已設，四區齊備）
- 測試 124 → 139 全綠

### 2026-07-09 晚（頻道分類改善＋提醒持久化）

- `3453158` Johnny 重整輪入庫：雙頻道分流（channel_guard）、/時間池比對、/補填時間、
  月度池催填提醒、日期 7 天分頁＋模式按鈕、管理指令 UI 隱藏、7 份新文件、117 tests
- 公告頻道分離：新增 `ANNOUNCE_CHANNEL_ID`（未設定 fallback 時間資訊彙整），三種公告
  （排課需求／月度收集／正式通告）改發公告區；頻道語意 helper 集中在 channel_guard
- 月度池提醒持久化：`.state/time_collection.json`（重啟不失憶、通知不重發）；
  終態（收齊／逾期通知已發）自動 deregister，提醒 loop 不再對已結束收集重複查詢
- 文件同步：DISCORD_CHANNELS 頻道表 2→4（公告區＋營運內部）、Kickoff 六區對照、
  提醒流向表、私人區＝ephemeral 刻意決策；PLAYBOOK／COMMANDS／README／CLAUDE 同步
- 測試 117 → 124 全綠

### 2026-07-09（分支 `feat/p0-foundation`）

- 新增 `/時間池比對`：管理者選月份、需要時長，再手動多選藝人，直接看月度時間池共同空檔
- 比對來源改以 `Availability Slots` 的月度可配合時段為主；正式佔用會先扣除 Tasks 的精準時間
- 顯示共同時間窗與 30 分鐘切分的可開始時間；時長顯示改為小時（例：240 分鐘 → 4 小時）
- 新主流程定位：`/時間收集建立` → `/時間池比對` → 營運手動建立 Project / Task；舊 `/排課*` 流程保留給單一明確排課需求
- 管理端 slash commands 加上 Discord Administrator default permission，非控方不會在指令選單看到
- 月度時間收集加通知：藝人按完成時控方收到小通知，全員收齊時收到大通知；背景 loop 會 push 未填藝人並知會控方
- 月度填寫 UI 改為 Discord 分頁批次填寫：每頁 7 天，勾同一批日期後一次套用同一段時間；日期時間不同再分批，同日多段可重複勾同一天

### 2026-07-08（分支 `feat/p0-foundation`）

- `428e972` 功能與效能一次入庫：月度時間收集（週次/分頁/模式）、時間池去重壓縮＋預覽、
  撞課檢查、互動失敗修正（autocomplete 快取、Notion 節流、批次整理、單筆讀取、未來界限查詢）
- `ef64c0f` **Phase 0**：TenantConfig 設定物件（平展化第一塊）、Notion 連線/節流改
  per-token（兩租戶並行實測通過）、快取加租戶維度、/時間池整理 TOCTOU 修正——行為零變化
- `ec0a1c8` **Phase 1a**：/我的任務（逾期→需修改→三天內→進行中→待審核 分區）、
  /任務總覽（含藝人名、涵蓋狀態空白的搬移舊資料）——全部 Notion 端過濾
- `81eb307` **Phase 1d**：照 Tasks「提醒規則」（3天前/1天前/當天）DM 提醒＋停機補跑；
  逾期不看規則一律觸發：DM 藝人、自動轉「已逾期」（持久化、重啟不重複）、升級管理層
- 測試 60 → **91 全綠**；Notion 五張 P0 表欄位與 config 全數核對一致
- Notion 文件同步：AI Ops Hub「目前必修」三項標 ✅＋進度更新；使用邏輯頁加更新紀錄

## 現在可實測清單（重啟 bot 後）

- [ ] `/我的任務`：藝人看到自己的任務分區（記得把要顯示的任務勾 `Discord 顯示` 並關聯藝人）
- [ ] `/任務總覽`：管理者看到全部未結案任務
- [ ] `/提交任務`：藝人選任務→依 Notion 指定方式貼連結／填文字／同頻道上傳 10MB 以下檔案→Notion Submissions 出現待審核、任務轉待審核、營運內部收到通知；再交一次舊版自動取消最新
- [ ] `/審核提交`：控方選待審核提交→通過／需修改／退回＋回饋→Submissions 寫審核欄位、Task 狀態同步、藝人收到 DM
- [ ] 任務提醒：設 `提醒規則` 或已過截止日的任務，30 分鐘內應觸發 DM／升級
- [ ] `/時間收集建立` → 藝人每頁 7 天批次勾選日期並套用時段 → 按完成觸發自動整理
- [ ] 藝人按月度填寫「完成」後，管理通知頻道收到單人完成通知；全員填完後收到可執行 `/時間池比對` 的通知
- [ ] 月度收集接近截止或逾期時，未填藝人收到 DM，控方收到未填名單
- [ ] `/時間池比對 月份:2026-08 時長分鐘:60` → 選 2 位以上藝人 → 看共同空檔與可開始時間
- [ ] `/時間池整理`：預覽 → 套用（資料變動會提示）
- [x] 舊排課流程從指令選單隱藏；目前改用 `/時間池比對` + Tasks 單一安排來源
- [x] 老師確認表單綁定課程 Task；老師多選後通知控方，正式時間仍由控方在 Tasks 完成

## 待 Johnny 提供／決定

- [x] ~~Drive 授權~~（2026-07-10 完成：token 已存、總資料夾可存取）
- [x] ~~藝人專區＋Notion 回填~~（Johnny／Chenhua 專區連結已填入 Artists）
- [ ] Tasks 42 筆舊資料人工確認 `Discord 顯示`（公司方作業）
- [x] 老師確認連結不列入目前主流程；正式網域不再是本階段阻塞項
- [ ] 財務核決模式與公司確認（單人 vs 多人；不阻塞前面 Phase）

## 新增資料庫（Phase 4/5 前設計評審）

現有 11 張正式表不含「合約義務」與「財務申請」——這兩張需新增，
schema 草案會先開給 Johnny 在 Notion 確認再實作。

## 風險備忘

- Notion 速率 ~3 req/s（per integration）是天花板；已用快取/節流緩解，
  賣錢版若遇瓶頸再加本地讀快取層（架構已預留）
- 排課流程超過 3 天即失效（Kickoff 紅線）——老師連結（Phase 2）是閉環關鍵
- 技術債備忘：repositories.py ~1,500 行（單一責任仍成立＝欄位對應，但建議 Phase 3 拆成 repositories/ 套件按資料庫分檔）
