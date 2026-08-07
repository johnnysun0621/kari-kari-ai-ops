# KariKari AI Ops Mermaid Flowchart

這份圖給人和 AI 協作者快速理解目前系統主流程。  
KariKari 是第一個測試站；流程設計仍以可平轉到其他公司為前提。

## 主營運流程

```mermaid
flowchart TD
    A["控方例會 / 月度規劃"] --> B{"是否已確定正式事項？"}

    B -->|已確定| C["在 Notion 建立 Project / Task"]
    C --> D["Bot 讀 Tasks / Projects 作為正式安排來源"]
    D --> E["藝人用 /我的任務 查看任務"]
    E --> F{"任務是否開放提交？"}
    F -->|否| G["只作為任務提醒 / 排程判斷"]
    F -->|是| H["藝人用 /提交任務"]

    H --> I{"Notion 指定提交方式"}
    I -->|檔案 10MB 以下| J["同頻道上傳附件"]
    I -->|Drive 連結| K["貼雲端連結"]
    I -->|文字| L["填文字內容"]
    I -->|混合| M["選檔案或連結 / 文字"]

    J --> N["Bot 歸檔到公司 Google Drive"]
    K --> O["Bot 寫入 Submissions"]
    L --> O
    M --> O
    N --> O
    O --> P["Task 轉待審核"]
    P --> Q["控方 / 老師用 /審核提交 或 Notion 審核"]
    Q --> R{"審核結果"}
    R -->|通過| S["Task 轉已完成"]
    R -->|需修改 / 退回| T["Task 轉需修改"]
    S --> U["Bot 通知藝人審核結果"]
    T --> U

    B -->|尚未確定| V["控方用 /時間資訊蒐集"]
    V --> W["藝人填月度可配合時段"]
    W --> X["Availability Slots 寫入時間池"]
    X --> Y["控方用 /從時間池建立任務"]
    Y --> Z["找共同空檔候選"]
    Z --> C
```

## 課程／錄音／通告安排流程

```mermaid
flowchart TD
    A["/從時間池建立任務\n取得共同空檔"] --> B{"安排類型"}
    B -->|課程| C["選課程類別、課程培訓 Project、老師、名稱"]
    C --> D["Bot 建立 Tasks\n類型：課程與作業"]
    B -->|錄音／通告／其他| E["控方手動在 Notion 建立 Project / Task"]
    D --> F["控方在 Tasks 補正式時間、作業與提交規則"]
    E --> F
    F --> G["藝人用 /我的任務 查看"]
    G --> H["/提交任務 → Submissions → 審核"]
```

## 資料庫責任分界

```mermaid
flowchart LR
    Artists["Artists\n身份與專區來源"] --> Discord["Discord\n操作入口"]
    Discord --> Availability["Availability Slots\n可配合時間池"]
    Discord --> Submissions["Submissions\n提交與檔案索引"]

    Projects["Projects\n上層歸屬"] --> Tasks["Tasks\n正式安排與任務來源"]
    Tasks --> Discord
    Tasks --> Submissions
    Submissions --> Tasks

    Tasks --> ScheduleCheck["正式時間衝突判斷"]
    Availability --> ScheduleCheck

    AuditLog["Audit Log\n重要操作紀錄"]
    Discord --> AuditLog
    Submissions --> AuditLog
    Availability --> AuditLog
```

## 快速判斷

- `Availability Slots`：只代表藝人可配合時間，不是正式行程。
- `Tasks`：正式安排、任務查詢、提醒、提交狀態的主要來源。
- `Projects`：上層歸屬與提醒來源，不直接封鎖整段時間。
- `Submissions`：每一次作業 / 作品提交與審核結果。
- 課程場次細節：集中記錄在 `Tasks` 的「課程與作業」任務，不再使用獨立資料庫。
