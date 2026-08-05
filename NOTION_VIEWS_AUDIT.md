# Notion Views 稽核

稽核日期：2026-08-05  
範圍：[KariKari Testing](https://app.notion.com/p/3965806f097b80e690afccf235effba8)

## 讀取原則

- Bot 只讀取 `.env` 指定的 Notion Data Source，並套用程式自己的查詢條件。
- Bot **不依賴任何人工 view 的 filter、sort 或 group**。
- 本輪只稽核，不修改 view 或定稿 schema。
- 筆數來自完整分頁的 read-only smoke test；`>100` 代表不能只看 Notion API 第一頁。

## 資料區總覽

| 區域 | 實際筆數 | 超過 100 | Bot 使用 | 用途 |
|---|---:|---:|---|---|
| Artists | 8 | 否 | 是 | Discord 身份、合約狀態、藝人關聯 |
| People | 8 | 否 | 是 | 老師、公司成員、審核人員 |
| Projects | 31 | 否 | 是 | Task 上層歸屬、課程培訓專案 |
| Tasks | 201 | 是 | 是 | 正式安排、任務、課程與作業唯一來源 |
| Tags | 31 | 否 | 否 | Notion 端治理／分類參考 |
| Course Meetings | 78 | 否 | 是 | 課程與會議紀錄、AI 知識來源 |
| Audit Log | 518 | 是 | 是 | Bot 重要操作與錯誤軌跡 |
| Availability Slots | 35 | 否 | 是 | 月度可配合時間池 |
| Submissions | 5 | 否 | 是 | 作業提交與審核紀錄 |
| Bulletin | 1 | 否 | 是 | Notion 公告與使用回饋 |

## Artists 區

| View | 類型 | 顯示欄位 | Filter / Sort / Group | 判讀 |
|---|---|---|---|---|
| 基本資料維護 | Table | 藝人名稱、本名、性別、負責人、溝通偏好、Drive、社群、Discord User ID、工作習慣、任務、合約狀態、專案、會議、提交、會議記錄、Artist ID，以及歷史排課 relation | 合約狀態列出全部選項；Artist ID 升冪 | 合約 filter 等同未篩選；`排課需求`、`正式課程` 是舊架構殘留，Bot 不讀 |
| 專案追蹤 | Table | 專案名稱、優先級、藝人、狀態、月份、起訖日、會議、負責人、任務、Drive、類型、ID | 依關聯藝人分組 | 藝人頁內查看所屬專案；Bot 不使用此 view |
| 任務追蹤 | Table | 名稱、狀態、類型、截止日、專案、Discord 通知／收到時間 | 依關聯藝人分組 | 藝人頁內快速追蹤；無排序，不能視為提醒順序 |
| 課程與作業 | Table | Task 全部課程、提交、審核、時間、老師欄位 | 任務類型=課程與作業；截止日升冪；依藝人分組 | 與現行 Task-first 流程一致 |
| 作業檔案索引 | Table | 提交、Drive、檔名、大小、審核、藝人、任務等欄位 | 依提交藝人分組 | 含 `NAS 路徑`、`關聯課程` 等未啟用欄位，Bot 不靠此 view 判斷最新版 |

## People 區

| View | 類型 | 顯示欄位 | Filter / Sort / Group | 判讀 |
|---|---|---|---|---|
| By 成員性質 | Table | 姓名、本名、性別、人員、聯絡、Drive、Discord、可見範圍、專案、藝人、身份、會議、ID | Person ID 升冪；依身份類型分組 | 老師與公司成員選單來源；實際權限仍由程式查 Data Source |

## Projects 區

| View | 類型 | 顯示欄位 | Filter / Sort / Group | 判讀 |
|---|---|---|---|---|
| 🚨總覽 | Table | 名稱、優先級、狀態、藝人、月份、起訖日、會議、人員、任務、完成日、Drive、類型、ID | 依專案類型分組 | 完整營運總覽，無日期排序 |
| 月度排程 | Table | 名稱、任務、優先級、狀態、截止日、人員、藝人、月份、ID、類型 | 類型=月度排程；月份升冪並分組 | 只看月度排程，不代表全部課程培訓 Project |
| Testing 首頁連結 view | Table | Project 全欄位 | 狀態=進行中；依類型分組 | 會隱藏尚未開始等專案，首頁不可當完整專案清單 |

## Tasks 區

| View | 類型 | 顯示欄位 | Filter / Sort / Group | 判讀 |
|---|---|---|---|---|
| 🚨總覽 | Table | 名稱、狀態、截止日、藝人、專案、類型、審核人員 | 排除完成／取消／封存；截止日升冪 | 主要待辦總覽；201 筆需注意 API 分頁 |
| 📁 依專案分組 | Table | 名稱、狀態、截止日、藝人、類型、開始日、審核人員、說明 | 排除完成／取消／封存；截止日升冪；依專案分組 | 適合例會按 Project 檢查 |
| 🏷️ 依類型分組 | Table | Task 全欄位 | 目前含空值條件、全狀態條件與空藝人條件；截止日升冪；依類型分組 | Filter 可能造成空結果或誤解，營運使用前應留意；本輪不改 |
| Testing 首頁連結 view | Table | 名稱、狀態、類型、截止日、專案 | 只含進行中／待提交／待審核／需修改／已逾期 | **會漏掉尚未開始的新任務**，首頁不可當完整通知依據 |

## Tags 區

| View | 類型 | 顯示／規則 | 判讀 |
|---|---|---|---|
| 所有標籤 | Table | 全部治理欄位 | Bot 不讀 Tags；僅供 Notion 人工治理 |
| 顏色分類 | Board | 名稱、說明；依任務性質大類 | 視覺分類 |
| 分類檢視 | Gallery | 名稱、說明 | 視覺瀏覽 |
| 任務性質｜Active | Table | 用途=任務性質、狀態=Active；依大類 | 可作人工標準字典，但不是 Bot 選單來源 |
| 治理總表｜依用途 | Table | 全欄位；依標籤用途 | 標籤治理主表 |
| Legacy｜藝人・人員・老師 | Table | Legacy 藝人／人員／合作方老師 | 歷史遷移參考，不是 People 權限來源 |
| 會議分類 | Table | 用途=會議分類 | 人工分類參考 |
| 課程分類 | Table | 用途=課程分類 | Bot 課程類別仍由程式常數及 Task schema 控制 |
| 待整理 | Table | 通用／待整理或用途空白 | 治理待辦 |

## Course Meetings 區

| View | 類型 | 顯示欄位 | Filter / Sort / Group | 判讀 |
|---|---|---|---|---|
| 會議記錄 | Table | 名稱、日期、摘要、建立者、類型、藝人、老師、專案、主題、藝人可見 | 日期降冪；依會議類型分組 | 完整主視圖 |
| 會議歸檔（依月份） | Table | 名稱、日期、摘要 | 日期降冪；依 Date 分組 | 月份歸檔瀏覽 |
| 藝人視角 | Table | 名稱、日期、類型、主題、摘要、老師、藝人、可見、專案 | 日期降冪；依藝人分組 | 仍須看 `藝人可見`；Bot 另做本人權限過濾 |
| 課程紀錄 | Table | 課程與會議欄位 | 會議類型=課程 | 課程助教的人工入口；Bot 不直接使用 view filter |

## Bot 自動化區

| 資料庫 / View | 類型 | 顯示欄位與設定 | 判讀 |
|---|---|---|---|
| Audit / Default view | Table | 全部 log 欄位；無 filter/sort/group | 518 筆且無時間排序，人工查錯時應主動按操作時間排序 |
| Availability / Default view | Table | 全部時段欄位；無 filter/sort/group | Bot 會自行限定藝人、月份與未來時間 |
| Availability / 表格 | Table | 同上；依藝人分組 | 適合人工抽查個人時間池 |
| Submissions / Default view | Table | 全部提交與審核欄位；無 filter/sort/group | `NAS 路徑`、`關聯課程` 是預留／歷史欄位；正式關聯來源是 Tasks |
| Bulletin / 未命名 view | Table | 名稱、狀態、日期、建立者、人員、類別、置頂 | 建議人工命名，但本輪不改；Bot 依資料欄位同步，不依 view |
| Bulletin browse / List view | List | Name、狀態、Date、Created by | 簡易瀏覽 view |

## 主要風險與結論

1. Tasks、Audit 已超過 100 筆，任何 API 程式都必須完整分頁；目前 repository smoke test 已驗證。
2. Testing 首頁的 Task view 不含 `尚未開始`，不能拿來判斷 Bot 是否漏通知。
3. Artists 的 `排課需求`、`正式課程` 與 Submissions 的 `關聯課程` 是舊架構文字；程式已不使用。
4. Tags 是人工治理資料，不是 Bot runtime contract，修改 Tags 不會自動改 slash command 選項。
5. View 設定可隨營運調整，不會影響 Bot；真正契約由 Data Source schema contract tests 保護。
