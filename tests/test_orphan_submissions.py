"""M4-20：任務刪了，它的提交也清掉；待審清單不再列出任務已刪除的提交。"""
import asyncio
import inspect
from datetime import datetime, timedelta
from types import SimpleNamespace

import config
from cogs import reminders as reminders_cog
from cogs import submissions
from services import audit, notion_api, repositories

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=config.TZ)


def page(page_id, *, created, task_ids=(), with_task_field=True, archived=False):
    props = {
        "提交名稱": {"type": "title", "title": [{"plain_text": f"提交{page_id}"}]},
        "審核狀態": {"type": "select", "select": {"name": "待審核"}},
    }
    if with_task_field:
        props["關聯任務"] = {"type": "relation", "relation": [{"id": t} for t in task_ids]}
    return {
        "id": page_id,
        "url": f"https://notion.so/{page_id}",
        "created_time": created.isoformat(),
        "archived": archived,
        "properties": props,
    }


def test_待審清單只查還有關聯任務的提交(monkeypatch):
    seen = {}

    async def query(ds, filter_=None):
        seen["filter"] = filter_
        return []

    monkeypatch.setattr(notion_api, "query_data_source", query)
    asyncio.run(repositories.get_reviewable_submissions())
    assert {"property": "關聯任務", "relation": {"is_not_empty": True}} in seen["filter"]["and"]


def test_任務已刪除的提交_放超過一小時才算(monkeypatch):
    pages = [
        page("old", created=NOW - timedelta(days=3)),
        page("fresh", created=NOW - timedelta(minutes=10)),
        page("no-field", created=NOW - timedelta(days=3), with_task_field=False),
        page("trashed", created=NOW - timedelta(days=3), archived=True),
    ]
    seen = {}

    async def query(ds, filter_=None):
        seen["filter"] = filter_
        return pages

    monkeypatch.setattr(notion_api, "query_data_source", query)
    orphans = asyncio.run(repositories.get_orphan_submissions(now=NOW))
    assert [s.page_id for s in orphans] == ["old"]
    assert seen["filter"] == {"property": "關聯任務", "relation": {"is_empty": True}}


def test_已經在垃圾桶的提交_再清一次不算失敗(monkeypatch):
    async def update(page_id, properties=None, archived=None):
        raise notion_api.NotionAPIError(400, "Can't edit block that is archived")

    monkeypatch.setattr(notion_api, "update_page", update)
    asyncio.run(repositories.trash_submission("p1"))


def _orphan(page_id):
    return repositories.SubmissionItem(page_id=page_id, name=f"提交{page_id}", status="待審核",
                                       url=f"https://notion.so/{page_id}")


def test_清理_移到垃圾桶並寫_Audit_一筆失敗不影響下一筆(monkeypatch):
    trashed, logs = [], []

    async def get_orphans():
        return [_orphan("a"), _orphan("b"), _orphan("c")]

    async def trash(page_id):
        if page_id == "b":
            raise RuntimeError("Notion 429")
        trashed.append(page_id)

    async def log_event(**kw):
        logs.append(kw)

    monkeypatch.setattr(repositories, "get_orphan_submissions", get_orphans)
    monkeypatch.setattr(repositories, "trash_submission", trash)
    monkeypatch.setattr(audit, "log_event", log_event)
    asyncio.run(reminders_cog.RemindersCog._cleanup_orphan_submissions(SimpleNamespace()))
    assert trashed == ["a", "c"]
    assert [log["event_name"] for log in logs] == ["清除提交｜提交a", "清除提交｜提交c"]
    assert all(log["action_type"] == "更新" for log in logs), "Audit Log 沒有「刪除」選項"


def test_清理查詢失敗_不擋後面的提醒(monkeypatch):
    async def boom():
        raise RuntimeError("Notion down")

    monkeypatch.setattr(repositories, "get_orphan_submissions", boom)
    asyncio.run(reminders_cog.RemindersCog._cleanup_orphan_submissions(SimpleNamespace()))
    assert "_cleanup_orphan_submissions" in inspect.getsource(reminders_cog.RemindersCog.check_loop.coro)


def test_附批改檔找不到資料夾_講出真正原因():
    gone = submissions.review_feedback_no_target_message([submissions.NO_TASK_REASON])
    assert "任務已經被刪除" in gone and "Drive 設定" not in gone
    assert "方晨" in submissions.review_feedback_no_target_message(["方晨"])
    assert "沒有連上" in submissions.review_feedback_no_target_message([])
