# 客戶需要提供的帳號與設定資料

這份文件給客戶與導入人員使用，說明 KariKari AI Ops 要開始設定時需要哪些帳號與權限。

核心原則：**客戶保留帳號所有權，我們只取得必要的協作權限，不收集客戶密碼。**

## 必要項目

### 1. Discord 伺服器

客戶需要提供：

- 要部署 Bot 的 Discord Server
- 一位能管理伺服器的管理員協助邀請 Bot
- 伺服器 ID
- 控方身分組，或允許 Bot 建立 `控方` 身分組
- 藝人身分組，或允許 Bot 建立 `藝人` 身分組

客戶不需要提供 Discord 帳號密碼。管理員只需要：

1. 邀請 Bot 進入伺服器。
2. 暫時授予初始化所需的管理頻道、管理身分組與管理應用程式指令權限。
3. 執行 `/伺服器初始化`。

Discord 頻道會依固定用途建立：公告區、時間管理、任務查詢入口、課程助教、營運內部通知、營運操作。詳細規則見 [SERVER_MIGRATION.md](SERVER_MIGRATION.md)。

## 2. Notion 工作區

客戶需要提供：

- KariKari 營運工作區
- Notion Integration 的連線權限
- 將 Integration 分享到 Bot 需要讀寫的正式資料庫或上層頁面

Bot 主要使用：

- Artists
- People
- Projects
- Tasks
- Availability Slots
- Course Meetings
- Submissions
- Audit Log
- 布告欄／留言板／使用回饋

客戶不需要提供 Notion 登入密碼。由客戶自行在 Notion 建立或授權 Integration，再把 `NOTION_TOKEN` 放入部署環境的 secret 或 `.env`。

## 3. 公司 Google Drive

如果要使用任務提交與老師回饋，客戶需要提供：

- 公司專用 Google 帳號，作為 Drive 檔案擁有者
- 一個公司根資料夾，供 Bot 建立藝人與任務資料夾
- 允許 Bot 透過 Google OAuth 讀寫該資料夾

建議使用公司帳號，不要使用工程師個人 Drive。客戶不需要提供 Google 密碼；由帳號持有人完成 OAuth 授權即可。

Bot 會在公司根資料夾下維護檔案，例如：

```text
公司根資料夾/
└── Johnny/
    └── 2026-08/
        └── 2026.08.15_測試課程/
```

Google Drive OAuth 細節見 [DRIVE_OAUTH_SETUP.md](DRIVE_OAUTH_SETUP.md)。

## 可選項目

### 4. LLM API 帳號

只有啟用小律（會議秘書）或營運 AI 查詢時才需要：

- 客戶自己的 LLM API 帳號
- API Key
- 使用的 API Base URL 與模型名稱

目前使用 OpenAI API 的 `gpt-5.6-luna`。API Key 只放在部署環境，不寫入 Notion、Discord 或 Git。

### 5. 固定部署主機

若 Bot 要長時間運作，客戶需要提供其中一種：

- 公司固定 Mac／Windows 電腦，保持開機並執行 Bot
- 雲端 VM 或部署平台

`/伺服器初始化` 的 Discord OAuth callback 只需要本機 `127.0.0.1` 可達，不需要對外網域或 Tunnel。

### 6. TimeTree

目前 TimeTree 沒有納入 Bot API 自動同步，因此不需要提供 TimeTree API 帳號。正式 Task 確認後，由控方人工補登 TimeTree。

## 客戶交付清單

開始設定前，請客戶確認：

- [ ] Discord Server 已準備完成
- [ ] Discord 管理員可協助邀請 Bot 與執行初始化
- [ ] Notion Integration 已建立並分享正式資料庫
- [ ] Notion 欄位與資料庫已定稿
- [ ] 公司 Google Drive 根資料夾已建立
- [ ] Google 帳號持有人可完成 OAuth
- [ ] 是否啟用小律（會議秘書）與 LLM API
- [ ] Bot 要部署在本機還是固定雲端主機
- [ ] 老師與藝人的 Discord User ID 已填入 Notion People／Artists
- [ ] 會收通知的公司同仁、審核人員的 Discord User ID 已填入 People
- [ ] Tasks 已建立 `提前交件`（Checkbox）、`提醒確認(From 藝人)`（Text）、`重送通知`（Checkbox）、`相關同仁`（Relation → People）

## 不要用這些方式交付

- 不要傳 Discord、Notion、Google 或 LLM 帳號密碼。
- 不要把 Token、Client Secret、API Key 貼在 Discord 或 Notion。
- 不要把 `.env`、OAuth token 或 credentials JSON 提交到 Git。
- 不要使用工程師個人 Google Drive 作為公司的正式檔案庫。

所有密鑰應由客戶或部署管理員直接放入本機 `.env`、部署平台 Secret，或公司密鑰管理服務。

## 最小啟動組合

若只先做 Discord + Notion 的 P0 測試，客戶只需準備：

1. Discord Server 與管理員協助。
2. Notion Integration 與正式資料庫分享權限。
3. Artists、People 的 Discord User ID。
4. 一台能持續執行 Bot 的電腦。

Google Drive 與 LLM API 可在需要檔案提交或小律（會議秘書）時再啟用。
