# 系統架構與流程圖

LURASUPA 營運系統的整體樣貌。**這份圖描述的是目前程式碼的實際行為**，不是規劃；
改功能時要一起更新，否則下一個接手的人會照著錯的圖做事。

最後校準：2026-09-24（M-1 藝人端資料隔離、M-2 時間回報可見性、T-5 提醒確認都已落地）

---

## 一、系統架構

三層：Discord 是**唯一**的操作入口，Bot 是邏輯層，Notion 是資料真相。

```mermaid
flowchart TB
    subgraph 使用者
        Admin["控方／老師"]
        Artist["藝人"]
    end

    subgraph DC["Discord（唯一入口）"]
        direction LR
        ChAdmin["營運操作<br/>營運內部通知"]
        ChPublic["公告區<br/>（藝人唯讀）"]
        ChArtist["時間管理<br/>任務查詢入口<br/>課程助教"]
    end

    subgraph Bot["Bot｜Python + discord.py"]
        direction TB
        Cogs["cogs／互動層<br/>scheduling_admin・scheduling_artist<br/>tasks_artist・submissions<br/>course_qa・bulletin・server_setup<br/>reminders（背景 loop）"]
        Svc["services／邏輯層<br/>repositories：Notion 欄位對應唯一出口<br/>permissions・task_notifications<br/>notion_links：藝人端零 Notion 入口<br/>overlap・timeparse・arrangement"]
        Cogs <--> Svc
    end

    subgraph Ext["外部服務"]
        Drive["Google Drive<br/>作業歸檔"]
        LLM["OpenAI<br/>小律問答"]
    end

    Notion["Notion｜9 個 Data Source<br/>Artists・Projects・Tasks<br/>Availability Slots・Submissions<br/>People・課程與會議記錄<br/>布告欄・Audit Log"]

    Admin --> ChAdmin
    Artist --> ChArtist
    ChPublic -.唯讀.-> Artist

    ChAdmin --> Cogs
    ChArtist --> Cogs
    Cogs --> ChPublic

    Svc <-->|"REST API<br/>節流 ~3 req/s"| Notion
    Svc --> Drive
    Svc --> LLM
```

**關鍵邊界**

- 藝人**只透過 Discord**取得資訊，拿不到任何 Notion 網址（見第四節）
- `repositories.py` 是中文欄位名 → API payload 的唯一對應處；Notion 改欄位名只改這裡
- 公司特定值（Data Source ID、頻道、用語、SLA）全部從 `config.get_tenant()` 注入

---

## 二、主流程：一個任務的一生

會議 2026-09-12 指定要呈現的那條線：**行政建任務 → 是否提前交件 → 派發／DC 顯示 → 提醒 → 完成**。

```mermaid
flowchart TD
    Start(["控方例會／月度規劃"]) --> Build["在 Notion Tasks 建任務<br/>或用 /時間交集查詢 建草稿"]
    Build --> Draft["草稿狀態<br/>Discord 顯示 = false"]

    Draft --> Fill["補齊：任務說明、關聯藝人<br/>截止日、提交方式"]
    Fill --> NeedEarly{"要先審核嗎？"}

    NeedEarly -->|"是<br/>勾 提前交件"| Early["死線 = 提交期限<br/>或 截止日 − 5 天"]
    NeedEarly -->|"否<br/>上課・通告"| Normal["死線 = 截止日"]

    Early --> Gate
    Normal --> Gate

    Gate{"勾 Discord 顯示？"}
    Gate -->|否| Hidden["藝人完全看不到<br/>也不佔用他的可配合時段"]
    Hidden -.控方補完再勾.-> Gate

    Gate -->|是| Dispatch["背景輪詢 ≤30 分鐘<br/>私訊每位關聯藝人"]
    Dispatch --> Notify["通知含：任務名・類型・死線<br/>任務說明・提交方式<br/>按鈕：收到任務 ／ 有問題"]

    Notify --> Ack{"藝人按收到任務？"}
    Ack -->|否| Nag["每 12 小時重送<br/>直到按下為止"]
    Nag --> Ack
    Ack -->|"有問題・無法配合"| Return["退回 尚未開始<br/>取消 Discord 顯示<br/>清空通知與提醒確認"]
    Return --> Fill
    Ack -->|是| Doing["任務狀態 → 進行中<br/>寫入 Discord 收到時間"]

    Doing --> Remind["截止前提醒<br/>依 提醒規則，留空＝死線當天<br/>附按鈕：收到通知"]
    Remind --> AckLog["藝人按下 → 寫入<br/>提醒確認(From 藝人)"]

    AckLog --> Submit{"死線前交了嗎？"}
    Remind --> Submit
    Submit -->|否| Overdue["任務狀態 → 已逾期<br/>私訊藝人（無按鈕）<br/>＋通知控方頻道"]
    Overdue -.控方追進度.-> Submit

    Submit -->|是| Review["任務狀態 → 待審核<br/>停止所有提醒"]
    Review --> Judge{"/審核提交"}
    Judge -->|通過| Done(["已完成"])
    Judge -->|"需修改／退回"| Fix["任務狀態 → 需修改<br/>寫入 修改期限 1～7 天"]
    Fix --> Refix["24 小時前提醒藝人<br/>逾時通知控方（狀態不變）"]
    Refix --> Review
```

### 死線判定（決定提醒與逾期發生在哪一天）

```mermaid
flowchart TD
    A{"任務狀態<br/>＝ 需修改？"}
    A -->|是| R["死線 = 修改期限<br/>其他日期全部不看"]
    A -->|否| B{"提前交件 ☑？"}
    B -->|是| C{"提交期限有填？"}
    C -->|是| D["死線 = 提交期限"]
    C -->|否| E["死線 = 截止日 − 5 天"]
    B -->|否| F{"是否開放提交 ☑<br/>且 提交期限有填？"}
    F -->|是| G["死線 = 提交期限"]
    F -->|否| H["死線 = 截止日"]
```

> ⚠️ 只填 `提交期限`、但 `提前交件` 和 `是否開放提交` 都沒勾 → **提交期限被忽略**，死線退回 `截止日`。
> 8 種情境的完整對照表在 [控方 SOP](../sop/ADMIN_SOP.md)。

---

## 三、月度排課流程

```mermaid
flowchart TD
    A["控方 /時間資訊蒐集"] --> B["公告區發公告<br/>＋私訊可排課藝人"]
    B --> C["藝人 /管理可配合時間<br/>勾日期 → 設時段 → 檢查 → 送出"]
    C --> D["寫入 Availability Slots"]

    C --> E{"撞到已派發任務？"}
    E -->|是| F["自動排除衝突時段<br/>只看已勾 Discord 顯示的任務"]
    F --> D

    D --> G{"全員填完？"}
    G -->|否| H["截止前 6 小時催一次<br/>逾期後再一次，共兩次"]
    H --> G
    G -->|是| I["通知控方可以排課"]

    I --> J["控方 /時間交集查詢<br/>唯讀看共同空檔"]
    J --> K{"要建任務？"}
    K -->|否| L(["關閉，不留痕跡"])
    K -->|是| M["選任務類型 → 課程分流或其他<br/>建立 Discord 顯示=false 的草稿"]
    M --> N(["接主流程：補內容 → 勾 DC 顯示"])

    A -.發錯了.-> X["/取消時間蒐集<br/>停催填＋停 SLA 升級<br/>不刪已填時段"]
```

> 控方端比對共同空檔時**會**把未勾 `Discord 顯示` 的草稿算成已佔用，避免把排掉的時段當空檔推出去；
> 藝人端則看不到草稿（見下一節）。兩邊用同一支函式、不同旗標。

---

## 四、藝人端資料邊界（2026-09-18 起）

Demo 要驗收的重點。**規則：藝人看得到的任何輸出，都不含 Notion 網址。**

```mermaid
flowchart LR
    subgraph N["Notion"]
        T["Tasks<br/>全部藝人的任務"]
        M["課程與會議記錄<br/>含公司內部會議"]
    end

    subgraph B["Bot 過濾層"]
        F1["Discord 顯示 ☑<br/>才派發給藝人"]
        F2["藝人可見 ☑<br/>＋會議類型排除清單"]
        F3["notion_links<br/>移除所有 Notion 網址"]
        F4["_artist_facing_description<br/>濾掉控方內部欄位"]
    end

    A["藝人<br/>（Discord）"]

    T --> F1 --> F4 --> F3 --> A
    M --> F2 --> F3 --> A

    T -.✗ 沒有連結可點.-x A
    M -.✗ 沒有連結可點.-x A
```

**為什麼連結不能給**：Notion 的逐頁權限管不到頁面上方的導覽列。藝人拿到任何一個頁面網址，
就能往上翻到整個資料庫——一筆任務的網址翻上去就是所有藝人的 Tasks。

已移除的 12 個入口：任務通知／未確認提醒的「查看任務內容」按鈕、按「收到任務」後的回覆、
審核結果與批改檔 DM、公告區的公開公告與布告欄同步、小律的附件索引／月份總覽／回答來源／LLM context。

守門測試：`tests/test_artist_no_notion_links.py`（每次跑測試都會擋）。

> 控方與老師的頻道**不受限**，`/審核提交` 詳情、營運內部通知仍保留 Notion 連結。

---

## 五、背景自動化（每 30 分鐘一輪）

`cogs/reminders.py` 的 `check_loop`，依序跑 8 項：

```mermaid
flowchart LR
    Loop(["check_loop<br/>REMINDER_INTERVAL_MINUTES"]) --> A1["月度時間池催填"]
    Loop --> A2["排課 SLA 升級<br/>48／72 小時"]
    Loop --> A3["任務派發通知<br/>勾 DC 顯示後"]
    Loop --> A4["課後作業通知<br/>勾 開放提交後"]
    Loop --> A5["未確認任務重送<br/>每 12 小時"]
    Loop --> A6["審核催辦<br/>24 小時後每 12 小時"]
    Loop --> A7["修改期限提醒<br/>24 小時前"]
    Loop --> A8["截止前提醒＋逾期升級"]
```

另有獨立的布告欄同步（預設 5 分鐘）與小律問答（即時觸發）。

**去重機制**：排課類在記憶體 `_sent`（重啟最多重發一次，可接受）；
任務逾期用 Notion 狀態 `已逾期` 持久化（重啟不重複轟炸）。

---

## 六、資料庫責任分界

```mermaid
flowchart LR
    Artists["Artists<br/>身份・Drive 專區"] --> Discord["Discord<br/>操作入口"]
    Discord --> Availability["Availability Slots<br/>可配合時間池"]
    Discord --> Submissions["Submissions<br/>提交與檔案索引"]

    Projects["Projects<br/>上層歸屬"] --> Tasks["Tasks<br/>正式安排・提醒來源"]
    People["People<br/>老師・審核人員"] --> Tasks
    Tasks --> Discord
    Tasks <--> Submissions

    Tasks --> Conflict["正式時間衝突判斷"]
    Availability --> Conflict

    Meetings["課程與會議記錄"] --> QA["小律問答"]
    Tasks -.反向追溯藝人可見範圍.-> QA

    Bulletin["布告欄"] <--> Discord
    Audit["Audit Log<br/>重要操作紀錄"]
    Discord --> Audit
    Submissions --> Audit
    Availability --> Audit
```

### 快速判斷

| 資料庫 | 是什麼 | 不是什麼 |
|---|---|---|
| `Availability Slots` | 藝人**可配合**的時間 | 不是正式行程 |
| `Tasks` | 正式安排、提醒、提交狀態的主要來源 | 不是草稿暫存區（未勾 DC 顯示才是草稿） |
| `Projects` | 上層歸屬與提醒來源 | 不直接封鎖整段時間 |
| `Submissions` | 每一次提交與審核結果 | 不覆蓋歷史版本（`是否最新版` 控制） |
| `課程與會議記錄` | 小律的唯一知識來源 | 不是藝人的 Notion 入口 |

---

## 待辦（會影響這份圖）

- `/從時間池建立任務` 已被 `/時間交集查詢` 完全覆蓋，預定移除（M-5）
- `/伺服器初始化` 預定從指令清單隱藏，程式保留供平轉用（M-6）
