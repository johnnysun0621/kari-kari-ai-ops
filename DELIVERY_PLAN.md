# v1.0 交付衝刺計劃（執行中）

> **給接手的 AI（Codex／Claude）**：這是 2026-07-17 與 Johnny 收斂核准的交付計劃。
> 依 D0→D8 順序執行；每個 D 完成即跑驗證、commit（訊息帶 D 編號）、更新本檔勾選與 ROADMAP 日誌。
> 開工前先讀 [CLAUDE.md](CLAUDE.md)（鐵則＋刻意設計清單）與 [HANDOFF.md](HANDOFF.md)。
> 需求源：7/11 會議「DC × Notion 功能展示與需求確認」（Notion Meetings 內）。

## 背景與已收斂決策

7/11 會議客戶對原型整體買單，提出補強清單；本計劃做完即正式交付 KariKari。

1. **排課流程**（會議決議取消「排課建立」）：改為 `/時間池比對` 勾選候選 → **幕後自動建 Schedule Request** → 產老師確認連結 → 老師手機點選 → 自動發通告。`/排課建立 /排課比對 /排課確認 /排課狀態` 從指令選單隱藏（程式與測試保留）
2. **洽談中藝人權限**：與已簽約相同；**未簽／暫停／封存無權限**（注意：現行程式「未簽」也放行，要收緊）
3. **AI 問答進交付**：OpenAI 相容 adapter——DeepSeek（`https://api.deepseek.com`）／Ollama 本地（`http://localhost:11434/v1`）／其他便宜模型**同一介面**，TenantConfig 設定切換
4. 分工：社群頁／性別年齡欄位＝Johnny 在 Notion 做；錄音→逐字稿自動化、Notion AI 授權＝寫進 DELIVERY.md「後續藍圖」，不實作

## 可重用積木（先讀再寫，鐵則 #7）

| 積木 | 位置 | 用途 |
|---|---|---|
| pending-attachment 等附件模式 | [cogs/submissions.py](cogs/submissions.py) `PendingFileSubmission`＋`on_message` listener | D1 批改檔回傳、D2 上傳驗證都復用此模式（Cog 實例 dict＋過期，非全域） |
| Drive 歸檔路徑 | [services/drive_archive.py](services/drive_archive.py) 對應表＋`resolve_archive_target` | D1 加「老師回饋」資料夾、D2 資料夾連結 |
| Drive API | [services/drive_api.py](services/drive_api.py) `upload_submission_file`／`_find_or_create_folder_sync` | D2 需另加「列出資料夾檔案」查詢（驗證上傳用） |
| 老師連結 token | [services/teacher_confirm.py](services/teacher_confirm.py) | D4 直接重用（簽名/一次性/持久化都已完成） |
| 確認→發通告核心 | [services/scheduling_flow.py](services/scheduling_flow.py) `publish_confirmed_schedule` | D4 重用 |
| 建排課需求 | [services/repositories.py](services/repositories.py) `create_schedule_request`／`set_request_candidates` | D4 幕後建單 |
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
- [ ] 觸發點：附件 >10MB 被擋、或任務提交方式=Drive 連結 → 顯示「📁 取得上傳資料夾」按鈕
- [ ] find-or-create `專區/類型夾/YYYY-MM/` → 回資料夾連結（專區已分享給藝人）
- [ ] 「✅ 我已上傳」→ bot 驗證資料夾內有**按鈕產生時間之後**新增的檔案 → 寫 Submissions（資料夾＋最新檔連結、檔名、大小）＋任務轉待審核；驗不到 → 提示重試
- [ ] drive_api 加 `list_recent_files(folder_id, since_ts)`（asyncio.to_thread 包裝）

### D3｜時段整理與驗證（會議 #6 #7，~0.5 天）
- [ ] 送出前**重疊**偵測：與既有時段重疊 → 自動合併寫入並回報「已與既有時段合併為 X」（語意＝較寬窗勝出）
- [ ] 確認「完成」批次自動整理正常；若已被移除則接回

### D4｜排課流程調整（會議 #12，~1 天）——**先做（不確定性最大）**
- [x] `/時間池比對` 直通老師確認：新增 `MonthlyPoolTeacherConfirmView`（幕後建確認單＋候選寫入＋連結）；連結邏輯抽成共用 `_teacher_link_note`；指令加選填 課程類型/老師 參數
- [x] 舊四指令移出選單（`LEGACY_SCHEDULING_COMMANDS_VISIBLE` 開關）＋守護測試＋DISCORD_COMMANDS 標記
- [x] Tunnel 上線：cloudflared 已裝、tunnel 執行中、TEACHER_LINK_SECRET/BASE_URL 已設、web 服務啟動、經 Tunnel 端到端驗證（無效 token→錯誤頁）✅；**剩 Johnny 手機實測一輪**（時間池比對→勾候選→點連結→自動通告）

### D5｜洽談中權限（會議 #3，~0.25 天）
- [ ] `INACTIVE_ARTIST_STATUSES` 加「未簽」；文件明確「已簽約＋洽談中＝藝人指令全開」
- [ ] 測試更新＋NOTION_SCHEMA／DISCORD_COMMANDS 同步

### D6｜AI 問答（會議 #9，~2-3 天）
- [ ] `services/ai_engine.py`：OpenAI 相容 chat（httpx 直打 `/v1/chat/completions`，零新依賴）；TenantConfig 加 `ai_base_url`/`ai_api_key`/`ai_model`
- [ ] `services/ai_context.py`：依身分組裝 context（藝人＝自己任務＋自己課程＋`可給藝人查詢`=true 決策＋`AI 可查詢等級`允許的會議；控方全開）；**權限過濾在組裝端**（LLM 看不到無權資料）；截斷上限
- [ ] `cogs/ai_qa.py`：`/問 問題:<文字>`；可選 `AI_CHANNEL_ID` 限頻道；回答附來源；每用戶冷卻 30s；audit「AI 回答」；未設 ai_base_url → 回覆未啟用
- [ ] 護欄：max_tokens、逾時、錯誤不洩 context；純函式測試（context 權限過濾／prompt 組裝／冷卻），engine monkeypatch httpx

### D7｜正式伺服器移轉包（會議 #8，~0.5 天）
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
