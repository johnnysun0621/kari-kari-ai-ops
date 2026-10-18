# Project Principles

## AI 供應商

- 小律（會議秘書）與營運 AI 使用核准的 OpenAI 設定。
- LongCat 永久不採用，不得作為主要供應商、備援供應商或相容 fallback。
- URL 或模型名稱命中 LongCat 時，程式必須在網路請求送出前拒絕。

## Product Direction

This project is not a one-off KariKari custom bot.

KariKari is the first testing and implementation site, but the product should be designed as a reusable operations scheduling system that can be applied to multiple companies, teams, and workflows.

Every new feature should be evaluated against this principle:

> Can this be configured for another company without rewriting core business logic?

## Core Product Model

The reusable product is an operations scheduling layer with these modules:

- Time collection
- Availability pool cleanup and deduplication
- Candidate time matching
- Task drafts and formal arrangements
- Teacher candidate confirmation
- Announcement and notification
- Submission and review
- Governed AI retrieval
- Audit log

Discord is the current interaction layer.
Notion is the current operations data layer.
Neither should be treated as permanently hard-coded product assumptions.

## Configuration First

Prefer configuration over hard-coded company-specific logic.

Company-specific values should live in environment variables, config files, or schema mapping layers:

- Discord guild IDs
- Discord channel IDs
- Discord role IDs
- Notion data source IDs
- Notion property names
- Course or activity types
- SLA rules
- Collection windows and deadlines
- Terminology used in customer-facing text

Avoid adding new core logic that only works because KariKari happens to use a specific page, property name, role name, or business term.

## Stable Names, Replaceable IDs

When this product is moved to another company, most Discord and Notion IDs will change.
The product should rely on stable operational concepts and configurable IDs:

- A `時間管理`-style channel may have a different Discord channel ID in every server.
- A `任務查詢入口`-style channel may have a different Discord channel ID in every server.
- Admin roles, guild IDs, Notion data source IDs, and integration tokens are tenant-specific.
- The code should read those values from `.env` / tenant config, not hard-code KariKari IDs.

The reusable shape is the role of the resource, not the concrete ID.
For example: `TASK_CHANNEL_ID` means "the task lookup entry channel for this tenant"; it does not mean KariKari's current channel forever.

## Terminology

The current KariKari implementation uses terms like "藝人" because that matches the first customer workflow.

Long term, this concept should be treated as a generic schedulable participant:

- Talent
- Member
- Staff
- Participant

Use customer-specific terms in presentation/configuration, not in reusable domain logic where practical.

## Notion Schema Direction

The current repository layer maps directly to KariKari's Chinese Notion properties.

This is acceptable for the MVP, but future work should move toward schema mapping, such as:

- `artist_name_field`
- `discord_user_id_field`
- `contract_status_field`
- `availability_start_field`
- `availability_end_field`
- `schedule_status_field`

The goal is to let a new company connect its own Notion workspace by updating mapping/configuration rather than editing the core scheduling code.

## 分類與對照要靠穩定欄位（2026-10-13）

程式需要「依某個值決定行為」時（檔案類型、Drive 資料夾、公告、硬擋時段、不判逾期…），
要先問：**這個欄位的選項，客戶會不會隨手新增或改名？**

- **Projects 的 `專案類型`** 是穩定欄位：九個固定選項，24 個專案全部有填，97% 的任務有掛專案。
  以它為主要分類依據（M2-01 起即如此）。
- **Tasks 的 `任務類型`** 不穩定：173 筆任務有 107 筆沒填；客戶還會把案子名稱當成任務類型
  （`方晨_哈根達斯雪酥`、`方晨像素吉他周邊`、`方晨youtube直播`），每接一個新案子就多一個選項。
  依它寫死的對照表會一直需要改程式，而且 Notion 改名後**不報錯、靜默失效**
  （`方晨業配內容` 改名後，檔案類型與 Drive 資料夾都默默落到「其他」）。
- 因此：
  1. 新功能優先依 `專案類型` 判斷；`任務類型` 只當「比較具體時才採用」的第一層，對不到就退回專案類型。
  2. 需要「名稱」的地方（例如 Drive 資料夾）直接用 Notion 上的實體名稱（專案名稱），不要另外維護一張名稱對照表。
  3. 必須依 `任務類型` 運作的既有功能（`PUBLIC_TASK_TYPES`、`HARD_BLOCK_TASK_TYPES`）放在 `.env`，
     並在文件寫明「這個值沒填或填錯時，功能不會啟動」。
  4. 任何寫死的選項字串都要進 `tests/test_notion_option_values.py`；但守門測試擋不到「Notion 那邊改名」，
     所以每次動到 Notion 選項後，要實撈一次 schema 對照程式（`notion_api.retrieve_data_source()`）。

## 檔案歸檔要跟 Notion 結構一致（2026-10-13）

- Drive 資料夾分層照 Notion：**藝人 → 專案 → 任務 → 每次提交的版本**（規則頁 M4-07）。
  控方在 Notion 看到的結構，Drive 就是同一個結構，不需要另一套分類讓人對照。
- 資料夾以名稱 find-or-create，所以**改名等於開新夾**；第一次提交後把任務夾連結回寫到 Task，
  讓同一個任務之後的提交不受改名影響。
- Bot 只建資料夾、不設定分享權限。「誰看得到哪個資料夾」若要寫進 SOP，必須先確認 Drive 上的實際設定，
  不能把設計意圖寫成既成事實。
- 外部服務的授權要能長期運作：OAuth「測試中」的 7 天過期會讓交件整個停擺而且沒有任何告警，
  正式環境要用服務帳號（＋共用雲端硬碟）或發布為正式版的 OAuth。

## Discord Interaction Direction

Discord commands and UI should stay workflow-oriented rather than company-specific.

Good examples:

- Collect availability
- View my slots
- Clean availability pool
- Match candidate times
- Create a formal task
- Publish a formal notice

Avoid encoding company-specific processes into command behavior unless the behavior is configurable.

## Data Safety

Scheduling data can affect real people and paid work. Favor explicit, reversible operations.

Current principles:

- Preview destructive cleanup before applying it.
- Archive Notion pages instead of deleting them.
- Write audit logs for important changes.
- Keep user-submitted time private in Discord unless intentionally announced.
- Treat duplicate cleanup as deterministic and test-covered.

## Development Rules

When adding or changing features:

1. Keep KariKari working as the active test implementation.
2. Avoid making the core harder to reuse for a second company.
3. Put company-specific decisions behind config or mapping when the extra complexity is reasonable.
4. Add tests for scheduling rules, deduplication, matching, and parsing behavior.
5. Document product-level decisions that future agents or engineers need to preserve.

## Current State

KariKari is still the active P0 testing source.

The codebase is not fully multi-tenant yet. The next major productization step is extracting Notion property names and customer terminology out of `services/repositories.py` and related Discord presentation code into an explicit schema/config layer.

2026-10-13 補充：分類與對照逐步從 `任務類型` 移往 `專案類型`（見上方「分類與對照要靠穩定欄位」）。
已完成：發文待確認彙整（M2-05）、課程不判逾期（M5-03）、課後提醒老師（M5-01）。
已完成、待重啟上線：Drive 歸檔與檔案類型（M4-07、M4-16）。
