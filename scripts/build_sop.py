"""把 docs/sop/ops/、docs/sop/artist/ 的分頁合併成完整版 ADMIN_SOP.md、ARTIST_SOP.md。

分頁是唯一來源；完整版是產生出來的，不要直接改。改了分頁後執行：

    .venv/Scripts/python.exe scripts/build_sop.py

`--check` 只比對、不寫檔（測試用）：完整版跟分頁不一致就回傳非 0。
"""
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOP = ROOT / "docs" / "sop"
AUDIENCES = ("ops", "artist")
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
REQUIRED = ("title", "audience", "order", "rules", "summary", "updated")

OUTPUTS = {
    "ops": (
        SOP / "ADMIN_SOP.md",
        "控方操作 SOP",
        "LURASUPA 營運系統的控方操作手冊，照實際工作順序編排。每個流程都標明系統會在 Notion 寫什麼，出狀況時你知道去哪裡看。"
        "藝人看到的是什麼，見 [藝人 SOP](ARTIST_SOP.md)；工程端的設定見 [設定與技術細節](../operations/SETTINGS.md)。",
    ),
    "artist": (
        SOP / "ARTIST_SOP.md",
        "藝人操作 SOP",
        "你會用到的四件事：**回報時間、確認任務、交作業、問小律**。",
    ),
}


@dataclass
class Page:
    path: Path
    meta: dict
    body: str

    @property
    def order(self) -> int:
        return int(self.meta["order"])


def parse(path: Path) -> Page:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    match = FRONTMATTER.match(text)
    if not match:
        raise ValueError(f"{path.name}：開頭缺少 --- 標記區塊")
    meta = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    missing = [key for key in REQUIRED if key not in meta]
    if missing:
        raise ValueError(f"{path.name}：標記缺少 {', '.join(missing)}")
    return Page(path, meta, text[match.end():].strip() + "\n")


def load(audience: str) -> list[Page]:
    pages = sorted((parse(p) for p in (SOP / audience).glob("*.md")), key=lambda p: p.order)
    for page in pages:
        if page.meta["audience"] != audience:
            raise ValueError(f"{page.path.name}：放在 {audience}/ 但標記是 audience: {page.meta['audience']}")
    orders = [page.order for page in pages]
    if len(orders) != len(set(orders)):
        raise ValueError(f"{audience}/ 有重複的 order")
    return pages


def _demote(body: str) -> str:
    """分頁的 # 標題變成完整版的 ##，其餘往下一層；程式碼區塊裡的 # 不動。"""
    out, fence = [], False
    for line in body.split("\n"):
        if line.lstrip().startswith("```"):
            fence = not fence
        elif not fence:
            if re.match(r"#{1,5} ", line):
                line = "#" + line
            # 分頁在 docs/sop/<對象>/，完整版在 docs/sop/：相對連結少一層 ../
            line = re.sub(r"\]\(\.\./", "](", line)
        out.append(line)
    return "\n".join(out)


def _anchor(title: str) -> str:
    return re.sub(r"[^\w一-鿿\- ]", "", title).strip().lower().replace(" ", "-")


def build(audience: str) -> str:
    _, title, intro = OUTPUTS[audience]
    pages = load(audience)
    folder = f"{audience}/"
    lines = [
        f"# {title}",
        "",
        f"<!-- 這個檔案由 scripts/build_sop.py 從 docs/sop/{folder} 產生，不要直接改；改分頁後重新執行。 -->",
        "",
        intro,
        "",
        "## 目錄",
        "",
    ]
    for page in pages:
        lines.append(f"- [{page.meta['title']}](#{_anchor(page.meta['title'])})：{page.meta['summary']}")
    lines.append("")
    for page in pages:
        lines += ["---", "", _demote(page.body).rstrip(), ""]
    return "\n".join(lines).rstrip() + "\n"


def main(check: bool) -> int:
    stale = []
    for audience in AUDIENCES:
        path = OUTPUTS[audience][0]
        text = build(audience)
        current = path.read_text(encoding="utf-8").replace("\r\n", "\n") if path.exists() else ""
        if current != text:
            stale.append(path.name)
            if not check:
                path.write_text(text, encoding="utf-8", newline="\n")
    if check and stale:
        print("完整版跟分頁不一致，請執行 scripts/build_sop.py：" + "、".join(stale))
        return 1
    print("已更新：" + "、".join(stale) if stale else "完整版已是最新")
    return 0


if __name__ == "__main__":
    sys.exit(main("--check" in sys.argv))
