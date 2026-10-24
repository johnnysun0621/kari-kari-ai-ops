"""把 SOP 分頁產生成互動式 SOP 網站（兩份：藝人版、營運版）。

來源跟完整版 SOP 一樣是分頁（docs/sop/artist/、docs/sop/ops/），營運版另外加上
docs/delivery/CLIENT_ACCEPTANCE_SOP.md（照「## 」章節拆成小頁）。網站外觀在
scripts/sop_site/template.html，內容以 JSON 嵌進去；產生的網頁放 build/sop_site/，不進 repo。

    .venv/Scripts/python.exe scripts/build_sop_site.py

產生後交給 Claude 發布到原本的 Artifact 網址（網址不變）。
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_sop  # noqa: E402

TEMPLATE = Path(__file__).resolve().parent / "sop_site" / "template.html"
OUT = ROOT / "build" / "sop_site"
ACCEPTANCE = ROOT / "docs" / "delivery" / "CLIENT_ACCEPTANCE_SOP.md"

EDITIONS = {
    "artist": {"label": "藝人版", "groups": ["artist"]},
    "ops": {"label": "營運版", "groups": ["ops", "artist", "acceptance"]},
}
GROUP_LABELS = {
    "ops": ("營運操作", "建任務、派發、審核、早報、出問題怎麼查"),
    "artist": ("藝人看到什麼", "藝人收到的訊息與按鈕，營運可以對照"),
    "acceptance": ("客戶驗收", "驗收前準備、日常流程、驗收清單"),
}


def _rules(value: str) -> list[str]:
    return [r.strip() for r in value.strip("[] ").split(",") if r.strip()]


def _sop_pages(audience: str) -> list[dict]:
    pages = []
    for page in build_sop.load(audience):
        meta = page.meta
        pages.append({
            "id": f"{audience}-{page.order:02d}",
            "title": meta["title"],
            "summary": meta["summary"],
            "order": page.order,
            "rules": _rules(meta["rules"]),
            "updated": meta["updated"],
            "md": page.body,
        })
    return pages


def _acceptance_pages() -> list[dict]:
    """客戶驗收 SOP 沒有分頁標記：開頭（# 標題＋版本資訊）當首頁，每個「## 」章節一頁。"""
    text = ACCEPTANCE.read_text(encoding="utf-8").replace("\r\n", "\n")
    parts = re.split(r"(?m)^## ", text)
    head, sections = parts[0], parts[1:]
    updated = (re.search(r"發行日期：(\d{4}-\d{2}-\d{2})", head) or [None, ""])[1]
    title_match = re.search(r"(?m)^# (.+)$", head)
    pages = [{
        "id": "acceptance-00", "title": "關於這份文件", "order": 0,
        "summary": (title_match.group(1) if title_match else "客戶驗收 SOP"),
        "rules": [], "updated": updated, "md": head.strip() + "\n",
    }]
    for index, section in enumerate(sections, start=1):
        heading, _, body = section.partition("\n")
        title = re.sub(r"^\d+\.\s*", "", heading).strip()
        first = next((line.strip() for line in body.split("\n") if line.strip() and not line.startswith(("#", "|", "-", ">"))), "")
        pages.append({
            "id": f"acceptance-{index:02d}", "title": title, "order": index,
            "summary": first[:60], "rules": [], "updated": updated,
            "md": f"# {title}\n{body}",
        })
    return pages


def site_data(edition: str) -> dict:
    groups = []
    for key in EDITIONS[edition]["groups"]:
        pages = _acceptance_pages() if key == "acceptance" else _sop_pages(key)
        label, blurb = GROUP_LABELS[key]
        if edition == "artist" and key == "artist":
            label, blurb = "藝人操作 SOP", "回報時間、確認任務、交作業、問小律"
        groups.append({"key": key, "label": label, "blurb": blurb, "pages": pages})
    return {
        "edition": edition,
        "editionLabel": EDITIONS[edition]["label"],
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "groups": groups,
    }


def render(edition: str) -> str:
    template = TEMPLATE.read_text(encoding="utf-8")
    payload = json.dumps(site_data(edition), ensure_ascii=False).replace("</", "<\\/")
    title = f"LURASUPA SOP {EDITIONS[edition]['label']}"
    return template.replace("__SITE_TITLE__", title).replace("__SITE_DATA__", payload)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for edition in EDITIONS:
        path = OUT / f"{edition}.html"
        path.write_text(render(edition), encoding="utf-8")
        print(f"已產生：{path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
