# Google Drive 認證設定教學

讓 bot 能把藝人提交的檔案自動歸檔到公司 Google Drive。

## KariKari 現況（2026-10-18）

| 項目 | 狀態 |
|---|---|
| 認證方式 | **OAuth 使用者授權**（路線 A／B），授權帳號 `karikariproject@gmail.com`，token 在 `credentials/token.json` |
| OAuth app | Google Cloud 專案 `kali-bot-506310`，發布狀態 **實際運作中**（External、未送驗證、使用者上限 100） |
| 總資料夾 | 「藝人提交檔案專用（小律維護，勿動）」，擁有者 karikariproject，一般存取「限制」；ID 在 `.env` 的 `GOOGLE_DRIVE_ROOT_FOLDER_ID` |
| 藝人專區 | 7 位已簽約藝人各一個專區，直接以藝人名稱命名，連結已寫入 Notion Artists「Google Drive 資料夾」；舊連結備份在 `.state/artist_drive_folders_backup_20261018.json` |
| 分享 | 每個專區只分享給該藝人本人的 Gmail（編輯者）；**總資料夾不要分享**。Bot 不會自動設權限，要人工分享 |
| 舊總資料夾 | `1f3PApJAUQIZpesPUzXod1PkwbU_bGEZ0`（.env 內註解保留），舊檔不搬 |

事故紀錄：2026-08-22 起 token 失效（`invalid_grant`，專案當時在「測試中」，refresh token 7 天就過期），
Drive 交件全部失敗；2026-10-18 把 OAuth app 發布為正式並重新授權後恢復。

## 選哪一種認證

`services/drive_api.py` 的 `_credential_path()`：兩者都設定時服務帳號優先。

| | OAuth 使用者授權（KariKari 現用） | 服務帳號 |
|---|---|---|
| 會不會過期 | app **發布為正式**後不會 7 天過期；但約 6 個月完全沒使用、使用者撤銷授權、或改密碼（含 Gmail scope 時）仍會失效 | 不會 |
| 容量算在誰頭上 | 授權帳號本人的 Drive 容量 | **服務帳號沒有自己的儲存空間**：在個人 Gmail 的「我的雲端硬碟」裡上傳會失敗（`storageQuotaExceeded`），只能寫進 **Google Workspace 共用雲端硬碟**（付費） |
| 適用情境 | 個人 Gmail 帳號（KariKari 屬於這種） | 客戶有 Google Workspace、可以開共用雲端硬碟 |
| 費用 | 免費 | Workspace 按人收費 |

> 先前這份文件寫「服務帳號有自己的 15GB」是錯的（服務帳號已沒有可用的儲存空間），已更正。

程式對兩種情況都相容：Drive API 呼叫都帶 `supportsAllDrives=True`，總資料夾放在共用雲端硬碟也能用。

---

## 路線 S｜服務帳號（只適用 Google Workspace 共用雲端硬碟）

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

### 前提：總資料夾必須在共用雲端硬碟

服務帳號沒有儲存空間。總資料夾要建在 Workspace 的**共用雲端硬碟**裡，並把服務帳號
加為該共用雲端硬碟的成員（「內容管理員」以上）。放在個人「我的雲端硬碟」會建得了
資料夾、但上傳檔案失敗。

---

## 路線 A／B｜OAuth 使用者授權（個人 Gmail 用這條）

**一定要把 OAuth app 發布為正式**（「Google Auth Platform」→「目標對象」→「發布應用程式」），
否則 refresh token 7 天就過期。發布前「品牌塑造」要填齊：應用程式名稱、支援 email、
首頁網址、隱私權政策網址，以及「授權網域」。KariKari 用公開的 Notion 頁面當首頁與隱私權政策，
授權網域填完整子網域 `season-kryptops-56c.notion.site`（`notion.site` 是公開後綴，不能只填它）。
發布後授權畫面仍會顯示「Google 尚未驗證這個應用程式」，自用不需送審，點「進階」→「前往」即可。

### KariKari 歷史狀態（2026-07-10，已被上方「現況」取代）

| 項目 | 狀態 |
|---|---|
| OAuth 授權 | ✅ 已完成（完整 `drive` scope，token 在 `credentials/token.json`） |
| 總資料夾 | ✅ 「Kali Kali AI Ops Testing」，ID 已入 `.env` 的 `GOOGLE_DRIVE_ROOT_FOLDER_ID` |
| 藝人專區 | ✅ Johnny、Chenhua 兩區已建立（後續新增藝人未必同步更新這份記錄，請以 Notion Artists 為準） |
| Notion 回填 | ✅ 兩位藝人的 Artists「Google Drive 資料夾」欄位已填入專區連結 |
| 檔案上傳功能 | ✅ 已上線（`/我的任務` → `提交任務` 選任務後，使用 Bot 建立的公司 Drive 指定資料夾） |

之後**新增藝人**：Artists「Google Drive 資料夾」留空也可以，第一次交件時 Bot 會在總資料夾底下
用藝人名稱 find-or-create 專區並回寫連結；之後到 Drive 把該專區分享給藝人本人。

### 整體圖像

```
Google Cloud 專案（一次性）
  └─ OAuth 用戶端 → client_secret.json   ← 「這個 app 是誰」
        └─ 授權流程 → token.json          ← 「app 可以用誰的 Drive」
              └─ bot 用 token 操作 Drive：
                 總資料夾/藝人專區/專案夾/任務夾/ver.N
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

## 歸檔結構（bot 自動維護，2026-10-18 上線）

規則全文見規則頁 M4-07、M4-16。

```
總資料夾/
└─ <藝人專區>/                                 ← Artists.Google Drive 資料夾
   ├─ <專案名稱>/                              ← 任務的「關聯專案」名稱
   │  └─ <YYYY.MM.DD_任務名稱>/                 ← 日期取截止日，沒有用 Notion 建立日
   │     ├─ <任務名稱_YYYY.MM.DD_ver.N>/        ← 每次提交一個版本夾
   │     └─ 老師回饋/老師回饋_YYYY.MM.DD_ver.N/
   └─ 未分專案/                                ← 沒掛專案的任務
```

- 隱私：每個專區只分享給該藝人，藝人看不到彼此
- 任務的 `Google Drive Folder` 有值 → 該任務的提交直接歸那裡
- 專案夾名稱照 Notion 原字串（不安全字元換成 `_`、最多 70 字）；掛多個專案用第一個。
- 取代 2026-07-10 的「任務類型夾／YYYY-MM」結構；舊檔留在舊總資料夾，不搬。
- 第一次提交後 Bot 回寫 `Google Drive Folder`，所以同一任務之後一律回到同一個任務夾。
- 3 小時內重新進入同一筆任務的交件，沿用同一個 `ver.N`（詳見 SUBMISSION_FLOW.md）。
- **專案建好後盡量不要改名**：改名後，之後第一次提交的任務會在新名稱的夾，已交過的留在舊夾。
- 既有檔案不搬（2026-10-13 時沒有任何任務已回寫 `Google Drive Folder`）。
- Submissions 的 `檔案類型`：先看任務類型（課程時間&作業繳交→課程作業、創作交付→Demo、企劃提案→企劃書、錄音→錄音檔），對不到看專案類型（藝人外部商業合作／商務合作→業配素材、課程培訓→課程作業、內容企劃→企劃書、作品製作→Demo），都對不到 → 其他。

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
| 授權後過幾天 token 失效（`invalid_grant`） | Cloud 專案還在「測試中」→ refresh token 7 天就過期。把 app 發布為正式（見路線 A／B 開頭），再跑 `scripts/authorize_drive.py` 重新授權，**然後重啟 bot**（執行中的 bot 記憶體裡還是舊 token）。已是正式狀態仍失效 → 查是否超過 6 個月沒用、或授權在 Google 帳戶被撤銷 |
| 服務帳號上傳失敗 `storageQuotaExceeded` | 服務帳號沒有儲存空間，總資料夾不在共用雲端硬碟。個人 Gmail 改用 OAuth |
| check_drive 說看不到總資料夾 | OAuth：登入的帳號不是資料夾擁有者／沒被分享編輯權限，或 scope 還是舊的 `drive.file` → 刪 token.json 重跑授權。服務帳號：資料夾沒有分享給金鑰檔裡的 `client_email` |
| 「這個應用程式未經驗證」 | OAuth 路線才會看到，正常。自己的 app 點「進階」→「前往」即可 |
| 換公司（平轉） | 個人 Gmail 走路線 B 並發布為正式；有 Workspace 才考慮路線 S。每租戶一組 credentials 與總資料夾，互不共用 |

## 安全備忘

- `credentials/` 已 gitignore——`client_secret.json` 與 `token.json` **永不 commit**
- token 等同「該帳號的 Drive 完整權限」，只放在跑 bot 的機器上
- 撤銷授權：[Google 帳戶 → 安全性 → 第三方存取權](https://myaccount.google.com/permissions) 移除該 app
