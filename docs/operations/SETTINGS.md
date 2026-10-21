# 設定與技術細節（工程端）

控方 SOP 只寫操作；這頁記工程端才會碰的設定與排查方式。改 `.env` 後要重開 bot（`restart_bot.bat`）才會生效。

## 時間與行為設定（`.env`）

| 設定 | 目前 | 影響 |
|---|---|---|
| `REMINDER_INTERVAL_MINUTES` | 30 分鐘 | 所有自動通知的檢查頻率。改小＝反應快，但 Notion 請求變多 |
| `TASK_ACK_REMINDER_HOURS` | 12 小時 | 藝人沒按「收到任務」的重送間隔（**只在每日早報關掉時有效**） |
| `REMIND_BEFORE_DEADLINE_HOURS` | 6 小時 | 月度收集截止前多久催未填的人 |
| `REVIEW_REMINDER_AFTER_HOURS` | 24 小時 | 提交後多久才列入每日催辦 |
| `DAILY_NAG_HOUR` | 10 點 | 審核人員催辦、課後提醒老師每天幾點跑 |
| `DAILY_BRIEFING_ENABLED`／`DAILY_BRIEFING_HOUR` | true／9 點 | 藝人與同仁每日早報（M3-24）。false＝回到逐則通知 |
| `OPS_BRIEFING_ENABLED`／`OPS_BRIEFING_TIME` | true／09:30 | 營運早報（M3-25）。false＝回到逐則通知；時間要比藝人早報晚，才算得出「昨天的早報沒看」 |
| `TEACHER_REMINDER_AFTER_HOURS` | 2 小時 | 課程結束後多久第一次提醒老師 |
| `TEACHER_REMINDER_MAX_DAYS` | 7 天 | 最多提醒老師幾天，之後改列在營運早報 |
| `PUBLISH_CONFIRM_DIGEST_WEEKDAY`／`_HOUR`／`_AFTER_DAYS` | 週一／10 點／3 天 | 發文待確認發布：星期幾（0＝週一，`daily`＝每天）、幾點、截止日過幾天才列入。營運早報開著時只看星期幾與天數 |
| `PUBLIC_TASK_TYPES` | 通告、演出 | 會發公告區、會列進本月排程的任務類型。值必須是 Notion `任務類型` 既有的選項 |
| `FILE_UPLOAD_WAIT_MINUTES` | 180 分鐘 | 給 Drive 連結後的上傳有效期 |
| `BULLETIN_SYNC_INTERVAL_MINUTES` | 5 分鐘 | 布告欄同步頻率 |
| `DEFAULT_REMINDER_RULES` | 5 天前、3 天前 | 每個任務一律套用的提醒，勾選再往上加（2026-10-03 起） |
| `APPROVAL_LEAD_DAYS` | 5 | 提前交件任務沒填提交期限時，截止日往前推幾天當交件死線 |
| `MEETING_QA_ENABLED` | false | 小律會議查詢（M6-06，2026-10-18 起封存）。改回 true 並重開即恢復 |

## 頻道設定

- 頻道 ID 都在 `.env`；`/伺服器初始化` 完成後會回傳要填的 ID 清單。換到其他公司時頻道沿用相同用途命名，ID 改成該 server 的實際值。
- `TASK_CHANNEL_ID` 有設定時，只有 `/我的任務` 能在任務查詢入口用。
- `SCHEDULE_CHANNEL_ID` 有設定時，藝人時間管理只能在時間管理用。
- `ADMIN_COMMAND_CHANNEL_ID` 有設定時，管理端指令只能在營運操作用；留空時沿用 `ADMIN_NOTIFY_CHANNEL_ID`。
- `ADMIN_NOTIFY_CHANNEL_ID` 是控方通知的唯一出口（M3-25）。沒設定時控方通知只寫記錄檔，**不會改發到藝人看得到的頻道**。
- 要讓錯頻道完全看不到不相關的 slash command，要到 Discord App／Integration 的 command permission 設定頻道可見性。

## 排查

- **先看 bot 記錄檔**：`logs/bot.log`（5MB 輪替、保留 5 份）。畫面上「❌ …失敗，請稍後再試」是給使用者看的，真正原因（例外與行號）只在這個檔案。搜尋畫面那句話的關鍵字，下面幾行就是原因。
- **Notion 選項檢查**：bot 每次啟動會比對程式用來篩選的選項（`services/schema_check.py`）。篩選只要帶到不存在的選項，整個查詢就失敗，所以刪掉或改名「任務狀態」「審核狀態」這類選項，會讓對應功能整個停擺；對不上時 `營運內部通知` 會收到 ⚙️ 系統警告。
- **可排課藝人 0 位**且 Artists 資料都正確：確認 Notion integration 有 Artists 資料庫權限。
- **管理指令看不到**：bot 是否已重開並同步 slash commands、是否仍在該 guild。
- **首次啟用通知功能**時，bot 不會回頭轟炸既有任務；測試請建一筆新的測試任務。
- 去重與狀態檔都在 `.state/`（`task_notification_log.json` 等），重開 bot 不會重發。
- **每天一次的發送**（藝人／同仁早報、營運早報、每日催辦）的送達狀態在 `task_notification_log.json` 的 `daily_runs`（M3-11）：`attempts` 第幾次、`done` 今天是否完成、`delivered` 已送達的收件人。全部送達才算完成；失敗下一輪重試，同一天最多 3 次（`services/daily_runs.py` 的 `MAX_ATTEMPTS`）。audit 的「每日早報」「營運早報」「每日催辦」會寫第幾次、失敗幾位。

## 系統限制

| 項目 | 值 |
|---|---|
| Notion API 送出間隔 | 約每秒 3 次（Notion 官方限制，資料多時查詢會變慢） |
| 小律回答逾時 | 45 秒，失敗自動重試 1 次 |
| 小律單次讀取上限 | 16,000 字 |
| Discord 單則訊息 | 2000 字（超過自動分頁） |
