"""M3-22：在 Notion 勾「重送通知」批次重送。"""
import asyncio
from types import SimpleNamespace

from cogs import channel_guard
from cogs import reminders as reminders_cog
from cogs import tasks_artist
from services import audit, notion_api, repositories
from services.notion_api import NotionAPIError
from services.repositories import TaskItem


def task(page_id, visible=True, status="進行中"):
    return TaskItem(page_id=page_id, name=f"任務{page_id}", status=status, task_type=None,
                    artist_page_ids=["a1"], discord_visible=visible)


class Harness:
    def __init__(self, monkeypatch, flagged, clear_fails=()):
        self.events = []
        self.admin = []
        h = self

        async def get_flagged():
            return flagged

        async def clear(page_id):
            if page_id in clear_fails:
                raise RuntimeError("Notion 429")
            h.events.append(("clear", page_id))

        async def reset(page_id):
            h.events.append(("reset", page_id))

        async def send(bot, t):
            h.events.append(("send", t.page_id))
            return tasks_artist.DispatchOutcome(notifyable_artists=[object()], sent=1)

        async def try_send(channel, **kw):
            h.admin.append(kw["content"])

        async def noop(*a, **k):
            return None

        monkeypatch.setattr(repositories, "get_tasks_flagged_for_resend", get_flagged)
        monkeypatch.setattr(repositories, "clear_task_resend_flag", clear)
        monkeypatch.setattr(repositories, "reset_task_discord_notification", reset)
        monkeypatch.setattr(tasks_artist, "send_task_notification", send)
        monkeypatch.setattr(channel_guard, "try_send", try_send)
        monkeypatch.setattr(audit, "log_event", noop)

    def run(self):
        cog = SimpleNamespace(bot=None, _admin_channel=lambda: "admin")
        asyncio.run(reminders_cog.RemindersCog._check_batch_resend(cog))


def test_欄位還沒建時什麼都不做(monkeypatch):
    h = Harness(monkeypatch, None)
    h.run()
    assert h.events == [] and h.admin == []


def test_勾選的任務先取消勾選再重送_並回報控方(monkeypatch):
    h = Harness(monkeypatch, [task("1"), task("2")])
    h.run()
    assert h.events == [("clear", "1"), ("reset", "1"), ("send", "1"),
                        ("clear", "2"), ("reset", "2"), ("send", "2")]
    assert len(h.admin) == 1 and "任務1" in h.admin[0] and "任務2" in h.admin[0]


def test_沒開Discord顯示或已結案的取消勾選但不送(monkeypatch):
    h = Harness(monkeypatch, [task("1", visible=False), task("2", status="已封存")])
    h.run()
    assert ("send", "1") not in h.events and ("send", "2") not in h.events
    assert ("clear", "1") in h.events and ("clear", "2") in h.events


def test_取消勾選失敗就不送_避免每輪無限重送(monkeypatch):
    h = Harness(monkeypatch, [task("1")], clear_fails={"1"})
    h.run()
    assert h.events == []


def test_查詢時欄位不存在回None(monkeypatch):
    async def missing(*a, **k):
        raise NotionAPIError(400, "Could not find property with name or id: 重送通知")

    monkeypatch.setattr(notion_api, "query_data_source", missing)
    assert asyncio.run(repositories.get_tasks_flagged_for_resend()) is None
