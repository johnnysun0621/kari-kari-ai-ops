from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOP = ROOT / "docs" / "delivery" / "CLIENT_ACCEPTANCE_SOP.md"


def test_client_acceptance_sop_has_required_delivery_sections():
    text = SOP.read_text(encoding="utf-8")

    for required in (
        "文件版本：v1.0",
        "發行日期：2026-10-23",
        "## 3. 角色與責任",
        "## 5. 日常工作流程",
        "## 6. 常見異常與處理",
        "## 7. 客戶驗收清單",
        "## 8. 驗收結果與簽核",
        "## 9. 版本紀錄",
    ):
        assert required in text


def test_client_acceptance_sop_covers_every_customer_role_and_uat_area():
    text = SOP.read_text(encoding="utf-8")

    for role in ("營運／控方", "藝人", "相關同仁", "負責老師", "審核人員"):
        assert role in text
    for area in ("### A.", "### B.", "### C.", "### D.", "### E.", "### F.", "### G."):
        assert area in text
    assert text.count("- [ ] **") >= 25


def test_client_acceptance_sop_does_not_expose_engineering_secrets():
    text = SOP.read_text(encoding="utf-8")

    for forbidden in ("DISCORD_TOKEN", "NOTION_TOKEN", "CLIENT_SECRET", ".env", "refresh token"):
        assert forbidden not in text
