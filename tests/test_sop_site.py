"""互動式 SOP 網站（scripts/build_sop_site.py）：藝人版只有藝人頁，營運版三組都有。"""
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_sop_site", ROOT / "scripts" / "build_sop_site.py")
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)


def _data(edition):
    html = site.render(edition)
    raw = re.search(r'<script id="site-data" type="application/json">(.*?)</script>', html, re.S).group(1)
    return html, json.loads(raw.replace("<\\/", "</"))


def test_藝人版只有藝人頁_不含營運與驗收():
    html, data = _data("artist")
    assert [g["key"] for g in data["groups"]] == ["artist"]
    assert "營運內部通知" not in html, "藝人版不能出現營運才有的東西"


def test_營運版有營運_藝人_客戶驗收():
    _, data = _data("ops")
    keys = [g["key"] for g in data["groups"]]
    assert keys == ["ops", "artist", "acceptance"]
    acceptance = data["groups"][2]["pages"]
    assert acceptance[0]["title"] == "關於這份文件" and any(p["title"] == "客戶驗收清單" for p in acceptance)


def test_頁面編號不重複_內容不會提早結束_script():
    for edition in site.EDITIONS:
        html, data = _data(edition)
        ids = [p["id"] for g in data["groups"] for p in g["pages"]]
        assert len(ids) == len(set(ids))
        assert html.count("</script>") == html.count("<script")
