"""SOP 分頁的格式與完整版（scripts/build_sop.py）。"""
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_sop", ROOT / "scripts" / "build_sop.py")
build_sop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_sop)

# 控方、藝人看的 SOP 不寫工程細節；這些放 docs/operations/SETTINGS.md
ENGINEERING_WORDS = re.compile(r"\.env\b|\.state/|bot\.log|schema_check|_ENABLED\b|def |commit `")


def test_每頁標記齊全_資料夾與對象一致_順序不重複():
    for audience in build_sop.AUDIENCES:
        pages = build_sop.load(audience)
        assert pages, audience
        for page in pages:
            assert page.body.startswith(f"# {page.meta['title']}\n"), page.path.name


def test_完整版跟分頁一致():
    for audience in build_sop.AUDIENCES:
        path = build_sop.OUTPUTS[audience][0]
        assert path.read_text(encoding="utf-8").replace("\r\n", "\n") == build_sop.build(audience), (
            f"{path.name} 跟分頁不一致，請執行 scripts/build_sop.py"
        )


def test_SOP_不寫工程細節():
    for audience in ("ops", "artist"):  # 工程版（eng/）本來就寫程式細節
        for page in build_sop.load(audience):
            assert not ENGINEERING_WORDS.search(page.body), f"{page.path.name} 有工程細節，請移到 docs/operations/SETTINGS.md"


def test_程式碼區塊都有標類型():
    for audience in build_sop.AUDIENCES:
        for page in build_sop.load(audience):
            fence_open = False
            for line in page.body.split("\n"):
                if line.strip().startswith("```"):
                    if not fence_open:
                        assert line.strip() in ("```discord", "```notion", "```text", "```yaml", "```mermaid"), (
                            f"{page.path.name}：程式碼區塊要標 discord／notion／text：{line.strip()}"
                        )
                    fence_open = not fence_open


def test_藝人頁不出現控方才有的東西():
    for page in build_sop.load("artist"):
        assert "營運內部通知" not in page.body, page.path.name
        assert "notion.so" not in page.body and "notion.com" not in page.body, page.path.name


def test_SOP_的相對連結都指得到檔案():
    docs = list((ROOT / "docs" / "sop").rglob("*.md"))
    for md in docs:
        for target in re.findall(r"\]\(([^)#\s]+\.md)", md.read_text(encoding="utf-8")):
            assert (md.parent / target).resolve().exists(), f"{md.relative_to(ROOT)} 的連結指不到：{target}"
