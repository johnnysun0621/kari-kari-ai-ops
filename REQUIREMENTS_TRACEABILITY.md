# 會議需求追蹤矩陣

本表把客戶與朋友在三次主要會議提出的需求，對應到現行功能、資料來源及測試。後續決議優先於較早版本。

| 會議日期 | 客戶需求 | 最終決策 | 對應功能 | Notion 來源 | Discord 操作 | 測試案例 | 狀態 |
|---|---|---|---|---|---|---|---|
| 2026-06-29 | 排課從一週縮短到兩天 | 月度時間池＋Task 正式安排；TimeTree 人工補登 | 收集、整理、比對、SLA | Availability、Tasks、Audit | `/時間資訊蒐集`、`/時間池比對` | overlap、提醒與 SLA tests | 已完成 |
| 2026-06-29 | 藝人主動回填多天多時段 | Discord 分頁批次，同時允許不同日期不同時段 | 月度可配合時間 | Availability | `/補填時間`、`/我的時段` | availability UI / repository tests | 已完成 |
| 2026-06-29 | 正式事項與可配合時間分開 | Availability 不是承諾；Tasks 是唯一正式來源 | Task-first 安排 | Availability、Tasks、Projects | `/正式通告發布` | formal notice tests | 已完成 |
| 2026-06-29 | 未填與逾時要催收 | 24h 藝人期限、老師 24h、48h 升級、72h 失效警告 | Scheduling SLA | Audit、Availability、Tasks | 控方頻道＋DM | scheduling SLA tests | 已完成 |
| 2026-06-29 | 任務查詢、提交、審核 | 提交方式由 Notion 指定，Drive 儲存 | 任務完整生命週期 | Tasks、Submissions、People | `/我的任務`、`/提交任務`、`/審核提交` | submissions / review tests | 已完成 |
| 2026-06-29 | 合約、財務、NAS、分析 | 需先核准新資料模型，不硬塞進定稿 schema | 長期 Roadmap | 待設計 | 待設計 | 待規格化 | 保留，未取消 |
| 2026-07-11 | 例會後建立課程／錄音／通告 | 課程直接建 Task；其他先建隱藏草稿 | Task 草稿建立 | Tasks、Projects、People | `/時間池比對` | availability matching / task draft tests | 已完成 |
| 2026-07-11 | 老師可複選候選，控方最後定案 | Task token 表單；老師回覆不直接發通告 | 老師確認頁 | Tasks、Audit | Web 表單＋控方確認 | teacher confirmation tests | 已完成 |
| 2026-07-11 | 正式通告不能誤發 | 控方預覽後確認；Audit 防重複 | 正式通告 | Tasks、Audit | `/正式通告發布` | formal notice dedupe tests | 已完成 |
| 2026-07-11 | 藝人不可看到營運機密 | 不開放 Notion；Discord 依 ID、角色與頻道限制 | 權限層 | Artists、People | 個人 ephemeral／DM／私人 Thread | permissions tests | 已完成 |
| 2026-07-11 | 課程助教可查本人紀錄 | 開放式 RAG；每次同步，失敗用快取 | 課程 AI | Course Meetings、Tasks、Projects | 課程助教自然對話 | course QA / attachment tests | 已完成 |
| 2026-07-24 | 新任務要確認收到 | 每筆獨立確認；未確認每 6 小時重送原通知 | 任務派發 | Tasks、Audit | 「收到任務」按鈕 | task dispatch tests | 已完成 |
| 2026-07-24 | 大檔與多檔提交不要混亂 | 10MB 分界；藝人／任務／版本資料夾 | Drive 提交流程 | Tasks、Submissions、Artists | `/提交任務` | Drive upload / folder tests | 已完成 |
| 2026-07-24 | 資料夾名稱易辨識 | `YYYY.MM.DD_任務名稱`，有截止日優先 | Drive 命名 | Tasks | 自動產生 | drive archive tests | 已完成 |
| 2026-07-24 | 指定老師／公司成員審核 | 審核人員 relation 讀 People | 指定審核 | Tasks、People、Submissions | `/審核提交` | reviewer scope tests | 已完成 |
| 2026-07-24 | 審核與修改不能無限等待 | 審核 24h；需修改／退回後建立修改期限並提醒 | 審核 SLA | Tasks、Submissions、Audit | DM＋控方提醒 | review deadline tests | 已完成 |
| 2026-07-24 | 回饋可含文字、批改檔、Drive | 保留三種與不需批改檔；移除截圖佐證 | 審核回饋 | Submissions、Drive | 審核 action UI | review feedback tests | 已完成 |
| 2026-07-24 | TimeTree 既有習慣 | 無 API，控方人工補登；Notion 是正式資料 | 營運 SOP | Tasks | 無自動指令 | 人工驗收 | 已決策 |
| 2026-07-24 | AI 成本與錯誤可治理 | 系統＋個人每日 token 額度；使用回饋進布告欄 | AI governance | Course Meetings、Bulletin | 回報問題／`/營運查詢` | LLM usage / feedback tests | 已完成 |
| 2026-07-24 | 正式伺服器可移轉 | 初始化角色與五用途頻道；ID 回填 env | Server setup | 無 schema 變更 | `/伺服器初始化` | server setup tests | 已完成，待目標伺服器真機 |

## 需求來源

- [KariKari 會議記錄總表](https://app.notion.com/p/f255806f097b831281c581ec9fad71f9)
- [2026-07-11 會議](https://app.notion.com/p/c4e10480afab4a42b3a20b2cc418af9b)
- [2026-07-24 會議](https://app.notion.com/p/3a75806f097b80e19a10f389c2f25253)

## 版本規則

- 7/24 原始紀錄曾寫 24 小時未確認提醒；後續實測已決定改為每 6 小時掃描並逐筆重送。
- Schedule Requests、Course Sessions 已刪除；歷史文件或 relation 名稱不代表 runtime 仍使用。
- 長期需求保留在 Roadmap，但需另行核准 schema 後才能實作。
