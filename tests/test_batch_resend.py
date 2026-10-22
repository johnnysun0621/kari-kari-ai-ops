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

        async def send(bot, ts):
            h.events.append(("send", [t.page_id for t in ts]))
            return {t.page_id: tasks_artist.DispatchOutcome(notifyable_artists=[object()], sent=1) for t in ts}

        async def try_send(channel, **kw):
            h.admin.append(kw["content"])

        async def noop(*a, **k):
            return None

        monkeypatch.setattr(repositories, "get_tasks_flagged_for_resend", get_flagged)
        monkeypatch.setattr(repositories, "clear_task_resend_flag", clear)
        monkeypatch.setattr(repositories, "reset_task_discord_notification", reset)
        monkeypatch.setattr(tasks_artist, "send_task_notifications", send)
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
    # 先全部取消勾選、重設，再一起送（同一位藝人好幾筆會合併成一則，M3-27）
    assert h.events == [("clear", "1"), ("reset", "1"), ("clear", "2"), ("reset", "2"), ("send", ["1", "2"])]
    assert len(h.admin) == 1 and "任務1" in h.admin[0] and "任務2" in h.admin[0]


def test_沒開Discord顯示或已結案的取消勾選但不送(monkeypatch):
    h = Harness(monkeypatch, [task("1", visible=False), task("2", status="已封存")])
    h.run()
    assert not any(e[0] == "send" for e in h.events)
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


def test_剛重送的記下來_同一輪派發不再送一次(monkeypatch):
    import inspect

    h = Harness(monkeypatch, [task("1"), task("2", visible=False)])
    cog = SimpleNamespace(bot=None, _admin_channel=lambda: "admin")
    asyncio.run(reminders_cog.RemindersCog._check_batch_resend(cog))
    assert cog._resent_this_round == {"1"}
    # 批次重送排在派發前面（2026-10-22 UAT：同時勾 Discord 顯示和重送通知，A1 被送了兩次）
    source = inspect.getsource(reminders_cog.RemindersCog.check_loop.coro)
    assert source.index("_check_batch_resend") < source.index("_check_task_dispatch_notifications")
