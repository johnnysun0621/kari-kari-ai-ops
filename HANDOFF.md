# 協作者接手指南

這份文件給其他 AI、工程師或營運協作者接手本專案時使用。  
目標是避免新協作者只看單一檔案就誤改流程、資料層或租戶設定。

## 先讀順序

1. [README.md](README.md)：專案入口與啟動方式。
2. [PROJECT_PRINCIPLES.md](PROJECT_PRINCIPLES.md)：產品原則與平轉要求。
3. [ROADMAP.md](ROADMAP.md)：目前進度與下一步。
4. [DISCORD_CHANNELS.md](DISCORD_CHANNELS.md)：Discord 頻道組成與 slash command 分流。
5. [DISCORD_COMMANDS.md](DISCORD_COMMANDS.md)：每個 slash command 用途與 Notion 互動。
6. [NOTION_SCHEMA.md](NOTION_SCHEMA.md)：Notion 資料庫交互邏輯。
7. [ARCHITECTURE.md](ARCHITECTURE.md)：工程架構與程式責任切分。
8. [TESTING.md](TESTING.md)：測試與驗收方式。

## 目前產品方向

KariKari 是第一個測試租戶，但此專案不是一次性客製。

核心原則：

- 頻道名稱代表用途，Discord ID 可以換。
- Notion Data Source ID 可以換。
- 新功能要能平轉到其他公司，不要把 KariKari 的 ID 或特殊流程寫死。
- `Availability Slots` 是可配合時間，不是正式行程。
- 正式安排以 `Tasks` / `Projects` 為主。
- `Course Sessions` 目前保留給課程場次細節與舊排課流程相容。

## 目前主要流程

主流程：

1. 控方在 `時間資訊彙整` 執行 `/時間收集建立`。
2. 藝人填月度可配合時段。
3. 藝人可用 `/我的時段` 查看與刪除，或用 `/補填時間` 補填。
4. 控方用 `/時間池比對` 找共同空檔。
5. 控方在 Notion 建立正式 `Project` / `Task`。

任務流程：

1. 控方在 Notion 建立或更新 `Tasks`。
2. 需要藝人看到時，勾 `Discord 顯示`。
3. 藝人在 `任務查詢入口` 執行 `/我的任務`。
4. 控方在 `任務查詢入口` 執行 `/任務總覽`。

舊排課流程：

- `/排課建立`
- `/排課比對`
- `/排課確認`
- `/排課狀態`

這組仍保留，但不是月度時間池主流程。

## 開發前檢查

在動手前先確認：

- 目前 branch。
- `git status --short` 是否有使用者或其他 agent 的變更。
- `.env` 是否已存在，但不要輸出 token。
- Bot 是否正在跑。
- 這次變更是否需要重啟 bot。

常用指令：

```bash
git status --short
pgrep -fl "python.*bot.py"
.venv/bin/python -m compileall bot.py cogs services tests config.py
.venv/bin/python -m pytest tests/ -q
```

## 編輯規則

### 不要做

- 不要把 `.env` 裡的 token 或 ID 貼到回覆中。
- 不要硬刪 Notion page；資料刪除應 archive。
- 不要把 KariKari 的 Discord channel ID、role ID、guild ID 寫死到程式。
- 不要把 Notion token 或 Discord token 寫進文件。
- 不要未確認就改 Notion schema。
- 不要把舊 `/排課*` 流程移除；它目前仍是相容流程。
- 不要把 `Availability Slots` 當正式行程。

### 應該做

- 改 slash command 時，同步更新 [DISCORD_COMMANDS.md](DISCORD_COMMANDS.md)。
- 改頻道分流時，同步更新 [DISCORD_CHANNELS.md](DISCORD_CHANNELS.md)。
- 改 Notion 讀寫欄位時，同步更新 [NOTION_SCHEMA.md](NOTION_SCHEMA.md)。
- 改營運流程時，同步更新 [OPERATIONS_PLAYBOOK.md](OPERATIONS_PLAYBOOK.md)。
- 改架構或模組責任時，同步更新 [ARCHITECTURE.md](ARCHITECTURE.md)。
- 改驗收流程時，同步更新 [TESTING.md](TESTING.md)。
- 改產品方向或平轉原則時，同步更新 [PROJECT_PRINCIPLES.md](PROJECT_PRINCIPLES.md)。
- 改進度或下一步時，同步更新 [ROADMAP.md](ROADMAP.md)。

## 程式責任邊界

| 要改的東西 | 優先看 |
|---|---|
| Discord slash command | `cogs/*.py` |
| 頻道限制 | `cogs/channel_guard.py` |
| 月度時間 UI / 文案 | `services/time_collection.py`、`cogs/scheduling_artist.py` |
| Notion 欄位 / CRUD | `services/repositories.py` |
| Notion API request | `services/notion_api.py` |
| 共同時段演算法 | `services/overlap.py` |
| 時間解析 / 顯示 | `services/timeparse.py` |
| Audit Log | `services/audit.py` |
| 租戶設定 | `config.py`、`.env.example` |
| 背景提醒 | `cogs/reminders.py` |

## 文件更新對照表

| 變更類型 | 必改文件 |
|---|---|
| 新增 / 修改 slash command | `DISCORD_COMMANDS.md` |
| 調整 Discord 頻道或權限 | `DISCORD_CHANNELS.md`、`OPERATIONS_PLAYBOOK.md` |
| 改 Notion 欄位或資料庫用途 | `NOTION_SCHEMA.md` |
| 改營運流程 | `OPERATIONS_PLAYBOOK.md` |
| 改工程架構 | `ARCHITECTURE.md` |
| 改測試方式或驗收情境 | `TESTING.md` |
| 改產品方向 / 平轉原則 | `PROJECT_PRINCIPLES.md` |
| 完成 phase 或新增下一步 | `ROADMAP.md` |

## 測試要求

程式行為變更至少跑：

```bash
.venv/bin/python -m compileall bot.py cogs services tests config.py
.venv/bin/python -m pytest tests/ -q
```

文件-only 變更可不跑完整測試，但至少要檢查相關文件連結與文字。

如果改 Discord 互動：

- 本機測試通過。
- 重啟 bot。
- 在 Discord 手動驗收正確頻道與錯誤頻道。

如果改 Notion 讀寫：

- 加純函式或 repository 層測試。
- 確認 Notion schema 文件更新。
- 實測 smoke test。

## 重啟 Bot

先查目前行程：

```bash
pgrep -fl "python.*bot.py"
```

停止舊行程後啟動：

```bash
.venv/bin/python bot.py
```

啟動後確認：

- Slash commands synced。
- Gateway connected。
- Notion smoke test 成功。

## 目前已知限制

- 尚未完整多租戶；目前一次跑一個 `TenantConfig`。
- Notion property names 仍偏 KariKari schema，未抽成 mapping。
- Discord command 在錯頻道「不出現」需要 Discord App / Integration 權限設定，不是程式可完全控制。
- 月度 active collection 進度提醒目前是 in-memory，Bot 重啟後可能需要重新發公告或靠既有資料判斷。
- Google Drive OAuth / 作業上傳流程尚未完成。
- 老師外部確認連結尚未完成。

## 接手時的安全提醒

- `.env` 是敏感檔，不要貼 token。
- Notion page archive 可以復原，delete 不應使用。
- 任何會影響正式行程的改動都要保留 Audit Log。
- 涉及藝人隱私的資料，Discord 預設用 ephemeral 或 DM，不要公開顯示個人完整時間池。
- 新功能先問：是否能套用到第二家公司？
