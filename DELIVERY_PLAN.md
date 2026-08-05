# v1.0 交付衝刺計劃（執行中）

> **給接手的 AI（Codex／Claude）**：這是 2026-07-17 與 Johnny 收斂核准的交付計劃。
> 依 D0→D8 順序執行；每個 D 完成即跑驗證、commit（訊息帶 D 編號）、更新本檔勾選與 ROADMAP 日誌。
> 開工前先讀 [CLAUDE.md](CLAUDE.md)（鐵則＋刻意設計清單）與 [HANDOFF.md](HANDOFF.md)。
> 需求源：7/11 會議「DC × Notion 功能展示與需求確認」（Notion Meetings 內）。

## 背景與已收斂決策

7/11 會議客戶對原型整體買單，提出補強清單；本計劃做完即正式交付 KariKari。

1. **安排流程**：`/時間池比對` 只產生共同空檔候選；課程由 Bot 依控方選擇建立一筆 `Tasks` 的「課程與作業」，錄音／通告／其他則由控方手動在 Notion 建立 Project / Task。正式時間、老師與作業資訊集中在 Tasks。
2. **洽談中藝人權限**：與已簽約相同；**未簽／暫停／封存無權限**（已完成收緊）
3. **AI 問答進交付**：OpenAI 相容 adapter——DeepSeek（`https://api.deepseek.com`）／Ollama 本地（`http://localhost:11434/v1`）／其他便宜模型**同一介面**，TenantConfig 設定切換
4. 分工：社群頁／性別年齡欄位＝Johnny 在 Notion 做；錄音→逐字稿自動化、Notion AI 授權＝寫進 DELIVERY.md「後續藍圖」，不實作

## 可重用積木（先讀再寫，鐵則 #7）

| 積木 | 位置 | 用途 |
|---|---|---|
| pending-attachment 等附件模式 | [cogs/submissions.py](cogs/submissions.py) `PendingFileSubmission`＋`on_message` listener | D1 批改檔回傳、D2 上傳驗證都復用此模式（Cog 實例 dict＋過期，非全域） |
| Drive 歸檔路徑 | [services/drive_archive.py](services/drive_archive.py) 對應表＋`resolve_archive_target` | D1 加「老師回饋」資料夾、D2 資料夾連結 |
| Drive API | [services/drive_api.py](services/drive_api.py) `upload_submission_file`／`_find_or_create_folder_sync`／`list_recent_files` | D2 驗證指定資料夾入口後新增檔案 |
| 任務建立 | [services/repositories.py](services/repositories.py) `create_formal_arrangement_task` | 課程比對後建立 Tasks；其他類型由控方手動建立 |
| 時段去重/合併 | repositories `create_availability_slot_if_absent`（exact 已擋）＋`compact_availability_slots` | D3 重疊偵測沿用其語意（較寬窗勝出、性質無關——刻意設計勿改） |
| 安全發送 | [cogs/channel_guard.py](cogs/channel_guard.py) `try_send` | 所有附帶通知 |
| 權限 | [services/permissions.py](services/permissions.py) | D5／D6 身分判斷 |
| web 確認頁 | [web/](web/) aiohttp（與 bot 同 process） | D4 已就緒，只差 Tunnel 真機驗收 |

## 任務清單

### D0｜起手（0.5 天）
- [x] Commit 工作區未入庫的公告 try_send 強化 — `96792f4`
- [x] 確認 D3 前置現況：自動整理仍運作（三個完成按鈕都掛 `_cleanup_notice_for_user`）；`/時間池整理` 在

### D1｜審核體驗補強（會議 #1 #2，~1 天）
- [x] `ReviewSubmissionSelect` 選項與 `ReviewActionView` 訊息顯示：Drive 連結、文字內容摘要、Notion 提交頁連結、關聯任務連結（`SubmissionItem` 已有 drive_url/note/url/task_page_ids，純顯示層）
- [x] 老師回傳批改檔：審核選結果後加「附批改檔」→ 復用 pending-attachment（10MB 同標準）→ 歸檔 `藝人專區/老師回饋/YYYY-MM/`（drive_archive 對應表加項）→ DM 藝人（回饋文字＋批改檔連結）；連結併入 Submissions `回饋` 文字（不動 schema）；>10MB 引導控方貼連結

### D2｜大檔 Drive 資料夾流（會議 #4，~1 天）
- [x] 附件 >10MB 或任務提交方式=Drive 連結時，提供指定 Drive 資料夾與「我已上傳到 Drive」按鈕
- [x] find-or-create `專區/類型夾/YYYY-MM/`，回傳資料夾連結（專區已分享給藝人）
- [x] 驗證按鈕只讀取入口建立後新增檔案，寫入 Submissions（資料夾、File ID、檔名、大小）並將任務轉待審核；驗不到時提示重試
- [x] 審核管理補強：Tasks 支援 `審核人員`、`審核期限`、`修改期限`；提交後預設 24 小時審核期限，需修改／退回預設 48 小時修改期限，背景 loop 依期限提醒
- [x] `drive_api.list_recent_files(folder_id, since_ts)` 使用 `asyncio.to_thread`

### D3｜時段整理與驗證（會議 #6 #7，~0.5 天）
- [x] 送出／完成後沿用 `compact_availability_slots`，重疊時段合併、包含時段封存，並回報整理結果
- [x] `/我的時段`、月度收集「完成」與 `/時間池整理` 共用整理邏輯；管理者套用前重抓最新資料避免 TOCTOU

### D4｜安排流程調整（會議 #12，已完成）
- [x] `/時間池比對` 產生共同空檔候選；不直接發布正式通告
- [x] 課程選擇課程類別、課程培訓 Project、老師後，直接建立 Tasks 的「課程與作業」
- [x] 錄音／通告／其他維持控方手動建立 Project / Task
- [x] `Schedule Requests`、`Course Sessions` 已移至 Notion 垃圾桶；殘留關聯欄位已移除
- [x] 舊 `/排課*`、Schedule Request／Course Session repository、設定、token 分支與測試完整刪除

### D5｜洽談中權限（會議 #3，~0.25 天）
- [x] `INACTIVE_ARTIST_STATUSES` 加「未簽」；文件明確「已簽約＋洽談中＝藝人指令全開」
- [x] 補上未簽權限測試與 Artists 文件說明

### D6｜AI 問答（會議 #9，~2-3 天）
- [x] 課程助教沿用 OpenAI 相容 chat（httpx 直打 `/v1/chat/completions`）；TenantConfig 支援 base URL、API key、model 與 token 護欄
- [x] `cogs/course_qa.py`：課程助教自然語言入口與 `/課程問答`；依 Discord User ID 只讀該藝人可見的課程紀錄，回答附來源
- [x] 課程紀錄在組裝端依藝人權限過濾，正文／附件分開讀取並截斷；Notion 每次提問重新同步，失敗才使用快取
- [x] 護欄：`max_tokens`、每日 token 上限、逾時、錯誤不洩 context；使用量只寫 `.state/llm_usage.json`

### 2026-08-04｜對照 2026-07-24 會議決議

7/24 會議的主要 Action Items 已對照目前版本：

- [x] 任務派發即時通知與「收到任務」確認按鈕；目前每 6 小時掃描並重新推送未確認任務。
- [x] 指定審核人員；來源為 Notion「🧑🏻‍🏫 成員・老師・合作方」中 `身份類型=公司成員` 的成員。
- [x] 審核期限與修改期限；提交後預設 24 小時審核期限，需修改／退回後預設 48 小時修改期限，背景 loop 會提醒相關對象。
- [x] Google Drive 任務資料夾改為 `YYYY.MM.DD_任務名稱`，無截止日則使用 Notion 建立日。
- [x] 完全移除截圖佐證，保留文字回饋、批改檔與 Google Drive 連結。
- [x] 課程專案先由控方在 Notion 建立；`/時間池比對` 只產生候選，課程任務才由 Bot 建立，錄音／通告／其他仍由控方手動建立 Project / Task。
- [ ] 內部 TimeTree 與 Notion 的自動同步，列入後續評估，不阻塞目前交付。
- [ ] 正式伺服器的頻道、身分組初始化與移轉驗收，仍屬 D7。

目前版本與會議原始紀錄的差異：會議曾記錄「24 小時未確認再通知」，後續已依營運測試需求改為「每 6 小時掃描一次並重新推送」。

### 2026-08-05｜P0 定稿 Schema 對齊

- [x] Projects 改讀正式欄位 `開始日`。
- [x] Tasks 移除已刪除的 `課程項目性質` 契約；課程正式佔用改由 `課程類別`＋精確時間判定。
- [x] 課程紀錄依 Tasks 的 `課程與會議記錄` 反向追溯。
- [x] 老師選單支援 `老師` 與 `公司成員`；課程類別補上 `聽力訓練課`。

### D7｜正式伺服器移轉包（會議 #8，~0.5 天，待執行）
- [ ] `/伺服器初始化`：建四區頻道（公告區唯讀／時間資訊彙整／任務查詢入口／營運內部限管理）＋藝人/控方身分組＋輸出 .env 貼上清單
- [ ] `SERVER_MIGRATION.md`：建 server → 邀 bot → 初始化 → 換 .env → 重啟 → 驗收清單（兼平轉 onboarding）

### D8｜交付收尾（~1 天）
- [ ] 全量驗收：7/11 待辦清單逐項＋Kickoff DoD＋全指令真機一輪
- [ ] `DELIVERY.md`：功能總表（指令×角色）、架構圖（既有 Mermaid）、已知限制（10MB／trycloudflare 網址會變／Notion 3req/s）、後續藍圖（逐字稿自動化、Notion AI、正式網域、合約履約、財務審批）
- [ ] ROADMAP 收斂、`git tag v1.0-karikari`
- [ ] 正式伺服器切換演練（Johnny 建好後跑 D7 流程）

## 執行順序

D0 → **D4** → D1 → D2 → D3 → D5 → D6 → D7 → D8（合計 ~7-8 工作天）

## 待 Johnny（不阻塞開工）

- [ ] DeepSeek API key（或決定先用 Ollama 本地）
- [ ] `brew install cloudflared`（D4 驗收）
- [ ] 建正式 Discord 伺服器（D7 演練）
- [ ] Notion：社群頁欄位、性別／年齡

## 每個 D 的完成定義（DoD）

1. `.venv/bin/python -m compileall bot.py cogs services scripts tests config.py web` 通過
2. `.venv/bin/python -m pytest tests/ -q` 全綠（新功能配純函式測試；新增測試工廠前先 grep tests/）
3. `python -m pyflakes` 改動檔乾淨
4. 真機驗該功能（重啟 bot 用 `nohup .venv/bin/python bot.py >> bot.log 2>&1 &`）
5. Commit（訊息帶 D 編號）＋勾選本檔 checkbox＋ROADMAP 進度日誌一筆
