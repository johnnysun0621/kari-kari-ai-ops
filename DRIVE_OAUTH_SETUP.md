# Google Drive OAuth 設定教學

讓 bot 能把藝人提交的檔案自動歸檔到公司 Google Drive。
照本文操作一次約 5–10 分鐘；平轉到其他公司時照同一份教學重跑即可。

## 整體圖像

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

## 常見問題

| 症狀 | 原因／解法 |
|---|---|
| 授權後過幾天 token 失效 | Cloud 專案在「測試中」→ refresh token 7 天過期。OAuth 同意畫面 →「發布應用程式」轉正式（自用 app 不需送審），再重跑授權 |
| check_drive 說看不到總資料夾 | 登入的帳號不是資料夾擁有者／沒被分享編輯權限；或 scope 還是舊的 `drive.file` → 刪 token.json 重跑授權 |
| 「這個應用程式未經驗證」 | 正常。自己的 app 點「進階」→「前往」即可 |
| 換公司（平轉） | 走路線 B 全套；每租戶一組 credentials 與總資料夾，互不共用 |

## 安全備忘

- `credentials/` 已 gitignore——`client_secret.json` 與 `token.json` **永不 commit**
- token 等同「該帳號的 Drive 完整權限」，只放在跑 bot 的機器上
- 撤銷授權：[Google 帳戶 → 安全性 → 第三方存取權](https://myaccount.google.com/permissions) 移除該 app
