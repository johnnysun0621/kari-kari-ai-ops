# Google Drive 認證設定教學

讓 bot 能把藝人提交的檔案自動歸檔到公司 Google Drive。

**兩種認證方式，服務帳號優先於 OAuth（`services/drive_api.py` 的
`_credential_path()`）：** 兩者都設定時服務帳號贏，從 OAuth 切換過去不必先
刪舊 `token.json`。

| | 服務帳號（推薦） | OAuth 使用者授權（舊方式，仍相容） |
|---|---|---|
| 會不會過期 | **不會**——沒有同意畫面、沒有 refresh token | **會**：Google Cloud 專案在「測試中」狀態時，refresh token 7 天不活動就失效（這個專案已經踩過這個坑） |
| 容量算在誰頭上 | 服務帳號自己的 15GB 免費額度（個人 Gmail 帳號沒有 Shared Drive 可用，寫進共用資料夾的檔案所有權仍歸服務帳號） | 授權帳號本人的 Google 帳號容量（15GB 免費或付費 Google One 方案） |
| 設定時間 | 約 10 分鐘，一次性，之後不用再管 | 約 5 分鐘，但之後可能要重跑 |
| 適用情境 | **新設定一律用這個** | 僅既有環境尚未遷移時的相容路徑 |

---

## 路線 S｜服務帳號（推薦，新設定一律走這條）

### 一、建服務帳號

1. 開 [Google Cloud Console](https://console.cloud.google.com/) → 選專案（沒有
   Drive API 專案的話先建一個，並在「API 和服務」→「程式庫」啟用 **Google
   Drive API**）
2. 「API 和服務」→「憑證」→「建立憑證」→ **服務帳號**
3. 填名稱（例：`lurasupa-drive-bot`）→ 建立 → 不用額外授予專案層級角色，直接
   「完成」
4. 回到憑證清單，點剛建好的服務帳號 → 「金鑰」分頁 → 「新增金鑰」→
   **JSON** → 下載

### 二、放金鑰、分享資料夾

1. 下載的檔案改名放到專案：`credentials/service_account.json`
   （已在 `.gitignore`，永不入庫）
2. 金鑰檔裡有一個 `client_email`（長得像
   `lurasupa-drive-bot@<專案>.iam.gserviceaccount.com`）——**把這個 email
   加進總資料夾的共用名單，給「編輯者」權限**。沒有這一步，服務帳號完全看不到
   這個資料夾，`find_or_create` 會每次都失敗。
3. 總資料夾本身若還沒建，先在 Drive 手動建好，再分享給上面那個 email

### 三、設定 .env 並驗證

```bash
GOOGLE_DRIVE_ROOT_FOLDER_ID=<總資料夾網址 folders/ 後面那串>
GOOGLE_SERVICE_ACCOUNT_KEY_PATH=credentials/service_account.json
```

```bash
.venv/bin/python scripts/check_drive.py
```

成功會列出總資料夾名稱＋底下所有藝人專區的連結，跟 OAuth 路線驗證方式相同。

### 容量監控

個人 Gmail 帳號沒有 Shared Drive，服務帳號建立的檔案容量算在**服務帳號自己
的 15GB 免費額度**，不是總資料夾擁有者的個人容量。目前沒有自動監控這個數字；
若未來提交量大，之後要加一支定期檢查用量的腳本，或評估升級 Google Workspace
改用 Shared Drive（會移除這個 15GB 上限，但 Workspace 是按人收費的訂閱制）。

---

## 路線 A／B｜OAuth 使用者授權（舊方式，僅既有環境相容）

新設定不要走這兩條，直接用上面的路線 S。以下保留給還沒遷移、仍在用
`credentials/token.json` 的環境參考。

### KariKari 歷史狀態（2026-07-10，OAuth 時期）

| 項目 | 狀態 |
|---|---|
| OAuth 授權 | ✅ 已完成（完整 `drive` scope，token 在 `credentials/token.json`） |
| 總資料夾 | ✅ 「Kali Kali AI Ops Testing」，ID 已入 `.env` 的 `GOOGLE_DRIVE_ROOT_FOLDER_ID` |
| 藝人專區 | ✅ Johnny、Chenhua 兩區已建立（後續新增藝人未必同步更新這份記錄，請以 Notion Artists 為準） |
| Notion 回填 | ✅ 兩位藝人的 Artists「Google Drive 資料夾」欄位已填入專區連結 |
| 檔案上傳功能 | ✅ 已上線（`/我的任務` → `提交任務` 選任務後，使用 Bot 建立的公司 Drive 指定資料夾） |

之後**新增藝人**時的步驟：總資料夾底下建「<藝人>專區」→ 跑 `scripts/check_drive.py`
拿到連結 → 填入該藝人 Notion Artists 的「Google Drive 資料夾」欄位。完成。

### 整體圖像

```
Google Cloud 專案（一次性）
  └─ OAuth 用戶端 → client_secret.json   ← 「這個 app 是誰」
        └─ 授權流程 → token.json          ← 「app 可以用誰的 Drive」
              └─ bot 用 token 操作 Drive：
                 總資料夾/藝人專區/類型夾/月份夾
```

兩個檔案都放專案的 `credentials/`（已在 `.gitignore`，永不入庫）。

---

## 路線 A｜重用之前 smoke test 的用戶端（KariKari 適用，最快）

之前（2026-06-29）在 `~/Documents/Kali Kali AI Ops/` 做 Drive 上傳測試時
已經建過 Google Cloud 專案與 OAuth 用戶端，直接沿用：

1. 把舊專案的用戶端檔案複製過來：

   ```bash
   mkdir -p credentials
   cp ~/Documents/"Kali Kali AI Ops"/credentials/client_secret*.json credentials/client_secret.json
   ```

2. 跳到下面「[三、跑授權流程](#三跑授權流程)」。

> 為什麼 token 要重拿：舊 token 用的是 `drive.file` scope（只能碰 app 自己建的檔案），
> **看不到手動建立的藝人專區**；而且測試中的 Google Cloud 專案 refresh token 7 天就過期。

---

## 路線 B｜從零建立（新公司平轉用）

### 一、建 Google Cloud 專案與啟用 API

1. 開 [Google Cloud Console](https://console.cloud.google.com/) → 上方專案選單 → **新增專案**（名稱例：`KariKari-Ops`）
2. 左側「API 和服務」→「程式庫」→ 搜尋 **Google Drive API** → **啟用**

### 二、建 OAuth 用戶端

1. 「API 和服務」→「OAuth 同意畫面」：
   - User Type 選 **外部**（個人帳號只有這選項）→ 建立
   - 填 app 名稱、支援 email → 儲存
   - 「測試使用者」加入**擁有總資料夾的 Google 帳號**
2. 「API 和服務」→「憑證」→「建立憑證」→ **OAuth 用戶端 ID**：
   - 應用程式類型：**電腦版應用程式**
   - 建立後點「下載 JSON」
3. 下載的檔案改名放到專案：`credentials/client_secret.json`

### 三、跑授權流程

```bash
.venv/bin/python scripts/authorize_drive.py
```

- 會自動開瀏覽器 → 用**擁有總資料夾的帳號**登入
- 看到「Google 尚未驗證這個應用程式」→ 點「進階」→「前往（不安全）」（自己的 app，安全）
- 勾選同意 Drive 權限 → 完成後終端機顯示 `✅ Token 已儲存`

### 四、設定 .env 並驗證

```bash
# .env（KariKari 已填好，新租戶要改）
GOOGLE_DRIVE_ROOT_FOLDER_ID=<總資料夾網址 folders/ 後面那串>
GOOGLE_OAUTH_CLIENT_PATH=credentials/client_secret.json
GOOGLE_OAUTH_TOKEN_PATH=credentials/token.json
```

```bash
.venv/bin/python scripts/check_drive.py
```

成功會列出總資料夾名稱＋底下所有藝人專區的連結——**把各專區連結填入
Notion Artists 的「Google Drive 資料夾」欄位**，歸檔就接通了。

---

## 歸檔結構（bot 自動維護）

```
總資料夾/
  振華專區/            ← 手動建立、連結填入 Artists.Google Drive 資料夾
    課程作業/2026-07/   ← bot 自動 find-or-create（任務類型夾／月份夾）
    創作交付/2026-07/
  Chenhua專區/
```

- 隱私：每個專區只分享給該藝人（＋控方），藝人看不到彼此
- 特例：任務的 `Google Drive Folder` 欄位有值 → 該任務的提交直接歸那裡
- 對應表（任務類型→資料夾名）在 `services/drive_archive.py`，租戶可覆寫

## 檔案上傳流程（設計說明）

交件只有一種方式：上傳到公司 Google Drive（2026-10-11 起 `Tasks.提交方式` 停用）。  
藝人作業檔案統一走 Google Drive，避免 Discord 附件大小、Nitro 方案與 Bot 下載轉傳負載差異。

| 提交內容 | 入口 | 流程 |
|---|---|---|
| 檔案 | `/我的任務` → `提交任務` | 選任務 → Bot 建立指定 Drive 資料夾（入口 3 小時有效） → 藝人上傳 → 按「我已上傳到 Drive」→ 寫 Submissions |

- 藝人檔案不受 Discord 10MB 限制，直接放入 Bot 建立的公司指定資料夾
- 不接受藝人自己的雲端連結或文字表單；文字內容請存成檔案上傳
- 上傳失敗不寫入半套資料：Bot 會提示重新驗證或聯絡控方檢查公司 Drive；Drive 成功但 Notion 失敗時保留資料夾連結供營運補登
- 老師批改檔附件是獨立審核流程，不代表藝人端恢復 Discord 作業上傳
- 歸檔路徑邏輯在 `services/drive_archive.py`（純函式）；Drive API 操作在 `services/drive_api.py`

完整產品規則見 [SUBMISSION_FLOW.md](SUBMISSION_FLOW.md)。

## 常見問題

| 症狀 | 原因／解法 |
|---|---|
| 授權後過幾天 token 失效（`invalid_grant`） | OAuth 使用者授權的已知問題：Cloud 專案在「測試中」→ refresh token 7 天不活動就過期。治標：OAuth 同意畫面 →「發布應用程式」轉正式（自用 app 通常不需送審），再重跑授權。**治本：改走路線 S（服務帳號），徹底不會有這個問題**——這個 repo 2026-10-09 已經踩過一次才加的服務帳號支援 |
| check_drive 說看不到總資料夾 | OAuth：登入的帳號不是資料夾擁有者／沒被分享編輯權限，或 scope 還是舊的 `drive.file` → 刪 token.json 重跑授權。服務帳號：資料夾沒有分享給金鑰檔裡的 `client_email` |
| 「這個應用程式未經驗證」 | OAuth 路線才會看到，正常。自己的 app 點「進階」→「前往」即可 |
| 換公司（平轉） | 走路線 S 或路線 B 全套；每租戶一組 credentials 與總資料夾，互不共用 |

## 安全備忘

- `credentials/` 已 gitignore——`client_secret.json` 與 `token.json` **永不 commit**
- token 等同「該帳號的 Drive 完整權限」，只放在跑 bot 的機器上
- 撤銷授權：[Google 帳戶 → 安全性 → 第三方存取權](https://myaccount.google.com/permissions) 移除該 app
