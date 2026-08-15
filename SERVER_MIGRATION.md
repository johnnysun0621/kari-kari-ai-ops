# Discord 伺服器初始化與移轉 SOP

## 前置權限

Bot 在初始化時需要：

- 管理頻道
- 管理身分組
- 檢視頻道、傳送訊息、嵌入連結
- 管理訊息、建立與管理 Thread
- 使用應用程式指令

Bot 的身分組必須高於它需要管理或指派的 `控方`、`藝人` 身分組。

## 初始化

1. 邀請 Bot 到目標 Discord server。
2. 暫時給 Bot 上述管理權限。
3. 由伺服器管理員執行 `/伺服器初始化`。
4. 檢查預覽：`公告區`、`時間資訊彙整`、`任務查詢入口`、`課程助教`、`營運內部通知`、`營運操作`。
5. 按「確認初始化」。既有同名頻道會更新分類、主題與權限。
6. 將 Bot 回傳的 ID 填入該租戶 `.env`，再重啟 Bot。

初始化採預設拒絕：未取得 `藝人` 或 `控方` 身分組的人看不到營運頻道；`營運內部通知` 與 `營運操作` 只有控方可見；`公告區` 對藝人唯讀。

## `.env` 對應

```env
GUILD_ID=
ADMIN_ROLE_ID=
ANNOUNCE_CHANNEL_ID=
SCHEDULE_CHANNEL_ID=
TASK_CHANNEL_ID=
AI_CHANNEL_ID=
ADMIN_NOTIFY_CHANNEL_ID=
BULLETIN_CHANNEL_ID=

LLM_BASE_URL=https://api.longcat.chat/openai/v1
LLM_MODEL=LongCat-2.0
LLM_DAILY_TOKEN_LIMIT=0
LLM_PER_ARTIST_DAILY_TOKEN_LIMIT=0
```

`0` 代表 Bot 不啟用每日 token 上限，但仍記錄使用量；供應商帳戶的額度與速率限制不會因此取消。`LLM_API_KEY` 另放部署平台 secret 或本機 `.env`，不要寫入文件。

不要把 `.env`、Discord token、Notion token 或 LLM API key commit 到 Git。

## Slash command 可見性

程式會在執行時阻擋錯誤角色與錯誤頻道；要讓不屬於使用者的 command 在輸入框中完全不出現，仍須人工設定：

1. 伺服器設定 → 整合 → Apps → 選 Bot。
2. `時間資訊彙整`：`/時間資訊蒐集`、`/管理可配合時間`。
3. `任務查詢入口`：只開放藝人 `/我的任務`；提交由該指令內按鈕進入。
4. `營運內部通知`：`/從時間池建立任務`、`/任務總覽`、`/任務通知重送`、`/審核提交`、`/營運查詢`、`/布告欄同步`；只允許控方。
5. `/伺服器初始化` 只允許伺服器管理員；因為它負責首次建立頻道，程式不硬性綁定營運內部通知，初始化後可在 Discord Integration 將它限制到營運內部通知。
6. 課程助教不使用 slash command，藝人在指定頻道直接對話。

## 身分與資料檢查

1. 在 Notion Artists 填入藝人的 Discord User ID。
2. 合約狀態為 `已簽約` 或 `洽談中` 才可使用藝人功能。
3. `未簽`、`暫停`、`封存` 必須被拒絕。
4. People 中老師／公司成員若需收到通知，填入 Discord User ID。
5. Discord 指派 `藝人` 或 `控方` 身分組。

## 真機驗收

1. 藝人只能看公告、時間、任務、課程助教，不能看營運內部通知。
2. 公告區不能由藝人發言，但公告按鈕可操作。
3. 時間指令在時間頻道可用，任務指令在任務頻道可用。
4. 課程助教可直接對話，內容只進入本人私人 Thread。
5. 控方能收到未填、SLA、提交與審核提醒。
6. 控方在 Notion 確認 Task 後勾選 `Discord 顯示=true`，Bot 自動發送藝人通知。
7. Notion 布告欄同步到 Discord 公告區。

## TimeTree

TimeTree 沒有納入 API 自動同步。正式 Task 確認後，由控方人工補登 TimeTree；補登不是 Bot 成功與否的判斷條件。

## 回復方式

- 頻道或角色設定錯誤：修正 `.env` 或 Discord 權限後重啟，不要刪 Notion 資料。
- 初始化結果不符：先人工修正或再次執行預覽；確認按鈕前不會修改。
- 程式版本回復：切回上一個已驗證 Git commit，保留 `.env` 與 `.state` 備份。
