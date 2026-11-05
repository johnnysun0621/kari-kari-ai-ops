"""M7-09：Audit Log 在 Notion 只留 90 天，舊的先存本機 CSV 再移到垃圾桶。"""
import asyncio
import csv
import json
from datetime import datetime

import config
from services import audit_retention, notion_api

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=config.TZ)


def page(page_id, created="2026-07-31T17:30:00.000Z", **extra):
    props = {
        "事件名稱": {"type": "title", "title": [{"plain_text": f"任務截止提醒｜{page_id}"}]},
        "操作類型": {"type": "select", "select": {"name": "提醒"}},
        "操作時間": {"type": "date", "date": {"start": "2026-08-01T01:30:00+08:00"}},
        "目標頁面 URL": {"type": "url", "url": "https://notion.so/x"},
        "前值": {"type": "rich_text", "rich_text": []},
        **extra,
    }
    return {"id": page_id, "created_time": created, "properties": props}


class FakeNotion:
    def __init__(self, monkeypatch, pages, trash_fails=()):
        self.pages = pages
        self.trashed = []
        self.query = {}
        fake = self

        async def query(ds, filter_=None, sorts=None, limit=None):
            fake.query = {"ds": ds, "filter": filter_, "sorts": sorts, "limit": limit}
            return fake.pages

        async def update(page_id, properties=None, archived=None):
            if page_id in trash_fails:
                raise RuntimeError("Notion 429")
            fake.trashed.append((page_id, archived))

        monkeypatch.setattr(notion_api, "query_data_source", query)
        monkeypatch.setattr(notion_api, "update_page", update)


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def test_只撈超過90天的_舊的先(monkeypatch, tmp_path):
    fake = FakeNotion(monkeypatch, [])
    asyncio.run(audit_retention.run_once(NOW, retention_days=90, archive_dir=tmp_path))
    assert fake.query["filter"] == {
        "timestamp": "created_time", "created_time": {"before": "2026-08-07T12:00:00+08:00"},
    }
    assert fake.query["sorts"] == [{"timestamp": "created_time", "direction": "ascending"}]
    assert fake.query["limit"] == audit_retention.BATCH_SIZE


def test_先存CSV再移垃圾桶_依台灣時間的月份分檔(monkeypatch, tmp_path):
    fake = FakeNotion(monkeypatch, [page("a"), page("b", created="2026-08-02T03:00:00.000Z")])
    result = asyncio.run(audit_retention.run_once(NOW, retention_days=90, archive_dir=tmp_path))
    assert (result.found, result.archived, result.trashed, result.failed) == (2, 2, 2, 0)
    assert fake.trashed == [("a", True), ("b", True)]
    rows = read_csv(tmp_path / "2026-08.csv")  # 7/31 17:30 UTC＝台灣 8/1 01:30
    assert [r["page_id"] for r in rows] == ["a", "b"]
    assert rows[0]["事件名稱"] == "任務截止提醒｜a" and rows[0]["操作類型"] == "提醒"
    assert rows[0]["目標頁面 URL"] == "https://notion.so/x"
    assert (tmp_path / "2026-08.csv").read_bytes().startswith(b"\xef\xbb\xbf"), "Excel 要看得懂中文"


def test_存檔失敗就不刪(monkeypatch, tmp_path):
    fake = FakeNotion(monkeypatch, [page("a")])

    def boom(pages, archive_dir=None):
        raise OSError("disk full")

    monkeypatch.setattr(audit_retention, "write_archive", boom)
    try:
        asyncio.run(audit_retention.run_once(NOW, retention_days=90, archive_dir=tmp_path))
    except OSError:
        pass
    assert fake.trashed == []


def test_移垃圾桶失敗下一輪再試_CSV不重複(monkeypatch, tmp_path):
    fake = FakeNotion(monkeypatch, [page("a"), page("b")], trash_fails={"b"})
    first = asyncio.run(audit_retention.run_once(NOW, retention_days=90, archive_dir=tmp_path))
    assert (first.trashed, first.failed) == (1, 1)
    fake.pages = [page("b")]
    FakeNotion.__init__(fake, monkeypatch, fake.pages)
    second = asyncio.run(audit_retention.run_once(NOW, retention_days=90, archive_dir=tmp_path))
    assert second.trashed == 1
    assert [r["page_id"] for r in read_csv(tmp_path / "2026-08.csv")] == ["a", "b"]


def test_預覽和停用都不動任何東西(monkeypatch, tmp_path):
    fake = FakeNotion(monkeypatch, [page("a")])
    preview = asyncio.run(audit_retention.run_once(NOW, retention_days=90, archive_dir=tmp_path, dry_run=True))
    assert preview.found == 1 and fake.trashed == [] and not list(tmp_path.iterdir())
    fake.query = {}
    off =asyncio.run(audit_retention.run_once(NOW, retention_days=0, archive_dir=tmp_path))
    assert off.found == 0 and fake.query == {}


def test_不認得的欄位收進其他欄位():
    row = audit_retention.row_for(page("a", 新欄位={"type": "number", "number": 3}))
    assert json.loads(row[audit_retention.EXTRA_COLUMN]) == {"新欄位": "3"}


def test_查詢只拿前幾筆(monkeypatch):
    calls = []

    async def request(method, path, body):
        calls.append(dict(body))
        return {"results": [{"id": str(i)} for i in range(100)], "has_more": True, "next_cursor": "c"}

    monkeypatch.setattr(notion_api, "_request", request)
    pages = asyncio.run(notion_api.query_data_source("ds", limit=150))
    assert len(pages) == 150 and len(calls) == 2 and calls[0]["page_size"] == 100


def test_archive資料夾不能進公開repo():
    from pathlib import Path

    ignored = (Path(__file__).resolve().parent.parent / ".gitignore").read_text(encoding="utf-8")
    assert "archive/" in ignored.splitlines()
