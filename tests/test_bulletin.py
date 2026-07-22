from datetime import datetime

import config
from cogs.bulletin import bulletin_fingerprint, format_bulletin
from services.repositories import BulletinItem


def _item(**changes):
    values = dict(
        page_id="page-1",
        url="https://notion.so/page-1",
        title="測試公告",
        category="📢 公告",
        status="未處理",
        date=datetime(2026, 7, 22, 10, 0, tzinfo=config.TZ),
        people=("Johnny",),
        pinned=False,
    )
    values.update(changes)
    return BulletinItem(**values)


def test_bulletin_fingerprint_changes_when_status_changes():
    assert bulletin_fingerprint(_item()) != bulletin_fingerprint(_item(status="已結案"))


def test_format_bulletin_contains_operational_fields_and_link():
    text = format_bulletin(_item(pinned=True))
    assert "📌 **測試公告**" in text
    assert "類別：📢 公告｜狀態：未處理" in text
    assert "人員：Johnny" in text
    assert "https://notion.so/page-1" in text
