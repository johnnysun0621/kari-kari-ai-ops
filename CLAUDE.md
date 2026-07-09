# CLAUDE.md — AI 協作接手指南

Discord 排課／營運 bot（Python + discord.py + Notion API）。KariKari 是第一個租戶，
最終要平展化成可賣給多間公司的產品。**你的任務通常是：接續 ROADMAP 的下一個 Phase。**

## 文件地圖（先讀這些再動手）

| 文件 | 內容 |
|---|---|
| [HANDOFF.md](HANDOFF.md) | AI／工程師協作者接手規則、改檔對照、測試要求 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 程式模組責任、資料邊界、平轉狀態 |
| [TESTING.md](TESTING.md) | 本機測試、Discord 手動驗收、Notion 驗收 |
| [ROADMAP.md](ROADMAP.md) | **進度總表＋下一步**＋進度日誌＋待 Johnny 提供事項——接手先看這裡 |
| [RESEARCH.md](RESEARCH.md) | 需求研究：Kickoff 客戶需求、11 張 Notion 資料庫欄位對應、Gap 分析、決策收斂紀錄 |
| [PROJECT_PRINCIPLES.md](PROJECT_PRINCIPLES.md) | 產品原則（多租戶平展化、資料安全）——Johnny 親寫，架構決策的最高依據 |
| [README.md](README.md) | 專案入口、安裝、.env 設定、常用文件 |

## 接手流程

1. 看 ROADMAP「Phase 進度總表」找下一個 🔜/⬜ 項目（含實作要點）
2. 依下方鐵則實作；重用 services/ 既有 helper（先 grep 再寫新的）
3. 驗證：`.venv/bin/python -m compileall bot.py cogs services tests config.py` ＋ `.venv/bin/python -m pytest tests/ -q`
4. Commit（訊息含 Phase 編號），並更新 ROADMAP 的進度總表與進度日誌

## 開發指令

```bash
source .venv/bin/activate
python -m compileall bot.py cogs services tests config.py
python -m pytest tests/ -q
python bot.py                      # 需 .env（見 README）
```

## 架構速覽

- [config.py](config.py)：`TenantConfig` dataclass ＝ 一間公司的全部設定；`get_tenant()` 取得。
  模組常數是向後相容別名，**新程式碼一律用 `get_tenant()`**
- [services/notion_api.py](services/notion_api.py)：Notion 薄封裝；**per-token 連線＋節流（~3 req/s）**
- [services/repositories.py](services/repositories.py)：**中文欄位名 → API payload 的唯一對應處**；
  Notion 改欄位名只改這裡。含 dataclass 模型、純函式（分區/去重/提醒階段）
- [cogs/](cogs/)：channel_guard（頻道分流＋公告頻道 fallback）、scheduling_admin（排課管理）、
  scheduling_artist（藝人填時段）、tasks_artist（任務查詢）、submissions（作業提交）、reminders（背景提醒 loop，30 分鐘一輪）
- 測試全是純函式測試（不打網路）；async 用 `asyncio.run` ＋ monkeypatch `notion_api.query_data_source`

## 鐵則（違反＝review 打回）

1. 公司特定值（DS ID、頻道、用語、SLA）從 `get_tenant()` 注入，不硬編碼
2. 不新增全域單例；快取／節流／連線以 token 或租戶資料源為 key
3. Notion 查詢一律帶 filter（關聯／狀態／`_future_date_filter`），**不撈全表再過濾**
4. 破壞性操作先預覽；封存（archive）不刪除；重要操作寫 `audit.log_event`
5. 互動處理第一行先 `defer()` 或立即回應（Discord 3 秒硬限）；autocomplete 必須走快取
6. 每個功能配純函式測試；新增測試工廠前先 grep `tests/`（曾發生同名 `task()` 遮蔽既有工廠）

## 刻意設計——看起來像 bug 但不是（別「修」它們）

- **時段去重是性質無關、較寬窗勝出**：`_slot_nature_priority` 恆回 1 是故意的；
  測試 `test_long_legacy_slot_replaces_contained_legacy_slot_without_priority` 明文保護
- **逾期提醒不看提醒規則一律觸發**（含規則空白的任務）；截止前提醒才照規則
- **date-only 截止日當天整天有效，隔天才算逾期**（`task_deadline_passed`）
- 任務「待審核」優先於「逾期」分區（已交出＝球在公司，不對藝人顯示逾期）
- 提醒去重：排課類在記憶體 `_sent`（重啟最多重發一次，可接受）；
  任務逾期用 Notion 狀態「已逾期」持久化（重啟不重複轟炸）

## Notion 資料層

- 正式資料層 11 張表（ID 內建在 config.py，與 Notion「工程設定區」核對一致）
- 速率 ~3 req/s per integration token——這是產品天花板，效能問題先想快取，不是拆節流
- 欄位級 schema 與功能對應：見 RESEARCH.md 第二節
- Audit Log 寫入失敗不阻斷主流程（audit.py 已包好）
- 月度收集進行中狀態持久化在 `.state/time_collection.json`（bot 本機狀態，非營運資料；已 gitignore）

## 目前狀態（2026-07-09）

分支 `feat/p0-foundation`；Phase 0、1a、1d 完成；月度時間池主流程、任務查詢、任務提醒、雙頻道分流已落地。  
**下一步 Phase 1c 審核流**（/審核：通過/需修改/退回＋DM 藝人）；1b 的檔案上傳待 Johnny 提供 Drive OAuth 憑證。細節見 ROADMAP。
