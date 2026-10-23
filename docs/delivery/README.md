# 客戶交付文件

這個資料夾放可直接交給客戶閱讀與驗收的正式文件；內容只描述使用方式、預期結果與驗收證據，不放程式碼、帳密或工程排查細節。

- [`CLIENT_ACCEPTANCE_SOP.md`](CLIENT_ACCEPTANCE_SOP.md)：客戶營運主版、角色說明、完整流程、異常處理、逐項驗收表與簽核區。
- [`../sop/ARTIST_SOP.md`](../sop/ARTIST_SOP.md)：藝人操作手冊。
- [`../sop/ADMIN_SOP.md`](../sop/ADMIN_SOP.md)：營運操作手冊。

正式驗收時，以本資料夾的文件作為入口；分頁 SOP 是功能細節的內容來源。每次功能行為改變，應先更新 `docs/sop/artist/` 或 `docs/sop/ops/`，重新產生完整版，再同步檢查本文件的驗收步驟。
