# KariKari v1.0 交付說明

## 已交付主流程

```text
Discord 收集月度可配合時間
→ 自動／人工整理重複時段
→ 控方比對共同空檔
→ 建立 Tasks 正式安排或草稿
→ 老師複選候選
→ 控方定案
→ 控方確認後勾選 Discord 顯示，自動通知藝人
→ TimeTree 人工補登
```

任務則由 Notion Tasks 派送到 Discord，支援逐筆收到確認、每 12 小時未確認重送、提交、Drive 歸檔、指定審核與修改期限。

目前程式註冊 10 個 slash commands；完整角色與頻道對應見 [DISCORD_CHANNELS.md](DISCORD_CHANNELS.md)。藝人檔案提交從 `/我的任務` 進入並統一使用 Google Drive；Discord 附件只保留在老師批改檔等獨立審核流程。

## 系統邊界

| 系統 | 責任 |
|---|---|
| Discord | 操作、通知、個人查詢、課程助教 |
| Notion | 正式營運資料與狀態 |
| Google Drive | 提交與批改檔案 |
| TimeTree | 控方人工補登 |

## 已知限制

- Discord slash command 的「完全隱藏」仍需在伺服器 Integrations 人工設定。
- TimeTree 無 API，未自動同步。
- 掃描型 PDF、舊 `.doc`／`.ppt`、加密附件不保證可抽取文字。
- 課程助教品質取決於 Notion 頁面正文、relation 與附件內容是否完整。
- 課程助教使用可切換的 OpenAI 相容端點；目前預設為 OpenAI `gpt-5.6-luna`。LongCat 僅在使用 LongCat endpoint 時關閉 thinking；Bot 端每日 token 護欄目前不限額，供應商帳戶額度與速率限制仍適用。
- 合約、財務、NAS、營運儀表板與多公司 onboarding 尚未建立資料模型。

## 上線與回復

- 上線前依 [SERVER_MIGRATION.md](SERVER_MIGRATION.md) 真機驗收。
- 每次部署先跑 [TESTING.md](TESTING.md) 的完整驗證。
- 發生回歸時切回上一個已驗證 commit；Notion 破壞性整理採封存而非刪除。
- 秘密只保留在 `.env` 或部署平台 secret，不進 Git。

## 稽核文件

- [會議需求追蹤矩陣](REQUIREMENTS_TRACEABILITY.md)
- [Notion Views 稽核](NOTION_VIEWS_AUDIT.md)
- [Notion Schema](NOTION_SCHEMA.md)
- [Discord 指令](DISCORD_COMMANDS.md)
- [營運 SOP](OPERATIONS_PLAYBOOK.md)
