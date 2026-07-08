# KariKari AI Ops Bot｜P0-1 排課 MVP

以 Discord 為入口、Notion 為營運資料層的排課系統。目標：把排課從 1 週以上的人工往返，壓縮到 **2 天內完成**。

> Product note: KariKari is the first test implementation. This project should evolve into a reusable operations scheduling system for multiple companies. See [PROJECT_PRINCIPLES.md](PROJECT_PRINCIPLES.md).

## 排課流程

1. 管理者 `/排課建立` → 選參與藝人 → 排課頻道公告 + DM 通知
2. 藝人點「📅 填寫可用時間」按鈕 → 填時段（可多筆）→ Bot 檢查 Tasks / Projects / Course Sessions 後寫入 Notion
3. 管理者 `/排課比對` → 自動算出全員共同時段 → 勾選候選（最多 5 個）
4. 管理者用既有管道（LINE 等）向老師確認
5. 管理者 `/排課確認` → 選定時段 + 填最終開始時間 → 自動建立正式課程、發布通告（頻道 + DM）
6. 背景任務：截止前 6 小時催收、逾期通知管理者、超過 48 小時自動標記 SLA 並升級

其他指令：`/排課狀態`（管理者總覽）、`/我的時段`（藝人查看／刪除自己填的時段）

## 安裝

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    Mac: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填入下方設定
python bot.py
```

## .env 設定

| 變數 | 說明 |
|---|---|
| `DISCORD_TOKEN` | Discord Developer Portal → Bot → Token |
| `GUILD_ID` | 伺服器 ID（開發者模式右鍵複製） |
| `SCHEDULE_CHANNEL_ID` | #排課 頻道 ID（公告與通告發布處） |
| `ADMIN_ROLE_ID` | 管理者身分組 ID（管理指令權限） |
| `ADMIN_NOTIFY_CHANNEL_ID` | 管理通知頻道 ID（留空則用排課頻道） |
| `NOTION_TOKEN` | Notion → Settings → Integrations 建立的 internal integration secret |

### Discord bot 需要的設定

- 邀請連結 scopes：`bot` + `applications.commands`
- Bot 權限：View Channels、Send Messages、Embed Links、Mention Everyone（點名用）
- 不需要 privileged intents

### Notion 需要的設定

1. 建立 internal integration（讀寫權限）
2. 把「KariKari 營運中心｜正式資料層」底下這些資料庫都 **Share → 加入該 integration**：
   Artists、Schedule Requests、Availability Slots、Course Sessions、Tasks、Projects、Audit Log
   （或直接 share 營運中心整頁，底下資料庫會一併授權）
3. Artists 的每位可排課藝人要填：`Discord User ID`（純數字），且 `合約狀態` 不要是 `暫停` / `封存`

Data Source ID 已內建在 `config.py`（與 Notion「KariKari 營運中心」頁的工程設定區一致），Notion 端重建資料庫時才需要在 .env 覆寫。

## 測試

```bash
pytest tests/ -v
```

啟動時 bot 會自動做 Notion 連線 smoke test（讀 Artists、寫一筆「Bot 啟動」Audit Log），看 console log 即可確認串接成功。

## Mac 常駐部署（launchd）

1. 把整個專案資料夾放到 Mac，例如 `~/karikari-bot`，照上面步驟建 venv 與 .env
2. 建立 `~/Library/LaunchAgents/com.karikari.bot.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.karikari.bot</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/你的帳號/karikari-bot/.venv/bin/python</string>
    <string>/Users/你的帳號/karikari-bot/bot.py</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/你的帳號/karikari-bot</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/你的帳號/karikari-bot/bot.log</string>
  <key>StandardErrorPath</key><string>/Users/你的帳號/karikari-bot/bot.log</string>
</dict>
</plist>
```

3. 啟用：`launchctl load ~/Library/LaunchAgents/com.karikari.bot.plist`
   （停用：`launchctl unload …`；看 log：`tail -f ~/karikari-bot/bot.log`）

## 專案結構

```
bot.py                    # 進入點：載入 cogs、sync 指令、persistent 按鈕、啟動 smoke test
config.py                 # .env、Notion Data Source ID、時區/SLA 常數
services/
  notion_api.py           # Notion REST 封裝（2025-09-03，含重試）
  repositories.py         # Notion 資料庫 CRUD（中文欄位名 → API payload 唯一對應處）
  overlap.py              # 共同時段交集演算法（純函式）
  timeparse.py            # Modal 輸入解析、候選時段文字格式
  audit.py                # Audit Log 寫入（失敗不阻斷主流程）
cogs/
  scheduling_admin.py     # /排課建立 /排課比對 /排課確認 /排課狀態
  scheduling_artist.py    # 填寫按鈕 + Modal + /我的時段
  reminders.py            # 催收與 SLA 背景任務（每 30 分鐘）
tests/test_overlap.py     # 演算法與解析單元測試
```
