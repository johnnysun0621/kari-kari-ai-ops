---
title: Notion 欄位對照
audience: eng
order: 6
section: 參考資料
rules: [M7-05]
summary: 九個資料庫中程式會讀寫的欄位、型別、誰寫、用在哪；Drive 路徑規則
updated: 2026-10-26
---

# Notion 欄位對照

欄位名稱必須完全一致，程式照名稱讀寫。2026-10-24 以 bot 的 Notion token 實撈對照過。

## Tasks

| 欄位 | 型別 | 誰寫 | 用途 |
|---|---|---|---|
| 任務名稱 | title | 營運；時間池 | 所有訊息的標題（前後空白去掉） |
| 任務狀態 | select | 營運；bot（收到、回報、交件、審核、逾期） | active：尚未開始、進行中、待提交、待審核、需修改、已逾期；結案：已完成、已完成發文、已封存（舊資料的已取消也算結案） |
| 任務類型 | select | 營運 | 公開類型（`PUBLIC_TASK_TYPES`）、硬擋時段（`HARD_BLOCK_TASK_TYPES`）、課程（`課程時間&作業繳交`）、檔案類型對應 |
| 課程類別 | select | 時間池 | 課程分支；有值的課程任務擋時段 |
| 開始日、截止日 | date | 營運；時間池 | 行程時間、追蹤期限、本月排程、課後提醒 |
| 提交期限 | date | 營運 | 第一輪交件期限 |
| 修改期限 | date | bot（審核）；營運 | 需修改時的期限；Notion 審核沒填補 3 天 |
| 提前交件 | checkbox | 營運 | 追蹤期限＝提交期限或截止日−5 |
| 是否開放提交 | checkbox | 營運 | 作業開放通知、可提交、是否自動逾期 |
| Discord 顯示 | checkbox | 營運；bot（回報問題時取消） | 派發開關（舊名 `Discord 可見` 也相容） |
| 重送通知 | checkbox | 營運；bot（取消勾選） | 批次重送 |
| 提醒規則 | multi_select | 營運 | 額外提醒階段：3 天前、1 天前、當天、逾期 |
| Discord 通知時間、Discord 收到時間 | date | bot | 派發完成、藝人收到 |
| 提醒確認(From 藝人) | rich_text | bot | 早報送出與已讀紀錄 |
| 關聯藝人 | relation → Artists | 營運；時間池 | 派發、早報、交件對象 |
| 相關同仁 | relation → People | 營運 | 知會、同仁早報 |
| 審核人員 | relation → People | 營運 | 交件通知、催辦（身份類型 公司成員／老師） |
| 負責老師 | relation → People | 時間池 | 課後提醒 |
| 關聯專案 | relation → Projects | 營運；時間池 | Drive 專案資料夾、專案類型 |
| 專案類型 | rollup | — | 課程培訓（不判逾期、課後提醒）、月度排程 （發文） |
| 課程與會議記錄 | relation | 營運 | 會議查詢（封存中） |
| Google Drive Folder | url | bot（單一藝人第一次交件）；營運 | 交件資料夾 |
| 最新提交 URL | url | bot | 最新 Submissions 頁 |
| Task ID | unique_id | Notion | — |

內頁：派發卡、早報、小律讀整頁；bot 會寫「藝人回覆」「Drive 資料夾索引」兩段（藝人端略過）。

## Submissions

| 欄位 | 誰寫 | 用途 |
|---|---|---|
| 提交名稱、提交藝人、關聯任務、提交時間 | bot | 交件紀錄（名稱格式 `藝人｜任務｜MM/DD HH:MM`） |
| 提交來源（Discord）、提交類型（檔案）、檔案類型、權限等級（藝人本人） | bot | 標記；權限等級不做任何過濾 |
| 是否最新版 | bot | 新交件時舊的改 false |
| 審核狀態 | bot；審核人 | 待審核 → 通過／需修改／退回 |
| 審核回饋、審核者、審核來源 | bot；審核人 | Notion 審核要至少一個有值才通知 |
| 審核通知時間 | bot | 已通知藝人 |
| Drive URL、Drive File ID、原始檔名、檔案大小 MB、備註 | bot | 檔案資訊 |

## 其他資料庫

| 資料庫 | 程式用到的欄位 |
|---|---|
| Artists | 藝人名稱（title）、Discord User ID、合約狀態（未簽／暫停／封存 視為不可排課、不能用小律）、Google Drive 資料夾 |
| People | 姓名 / 名稱、Discord User ID、身份類型（老師、公司成員） |
| Projects | 專案名稱、專案類型、專案狀態、關聯藝人、開始日、截止日 |
| Availability Slots | 可用時段名稱、填寫藝人、填寫人名稱、身份、來源、可用開始時間、可用結束時間、提交時間、是否逾期提交、備註 |
| Audit Log | 事件名稱、操作時間、操作類型、操作者、來源系統、結果、Discord User ID、目標資料庫、目標頁面 URL、前值、後值 |
| 布告欄 | Name、類別（🙋 使用回饋 不同步）、狀態、Date、人員、📌 置頂 |
| 課程與會議記錄 | Name、Date、會議類型、藝人可見、關聯專案（會議查詢封存中；程式讀的「參與藝人」「會議摘要」在客戶 Notion 不存在，恢復前要處理） |

## Drive 路徑（`services/drive_archive.py`）

1. Tasks `Google Drive Folder` 有值 → 直接用
2. 否則 Artists `Google Drive 資料夾` → `{專案名稱或未分專案}/{YYYY.MM.DD_任務名稱}`
3. 否則 `GOOGLE_DRIVE_ROOT_FOLDER_ID/{藝人名}/…`（不寫回 Artists）

交件夾 `{任務名稱}_{YYYY.MM.DD}_ver.N`；批改檔 `老師回饋/老師回饋_{YYYY.MM.DD}_ver.N`。名稱非法字元換 `_`，截 70 字；找或建依名稱比對。**程式不設定任何分享權限。**
