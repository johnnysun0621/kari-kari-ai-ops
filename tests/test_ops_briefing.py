"""控方每日營運早報（M3-25 第二階段）。"""
import asyncio
import dataclasses
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import discord
import pytest

import config
from cogs import briefing, ops_briefing as ops_cog, reminders as reminders_cog, tasks_artist
from services import audit, notification_log, ops_briefing, repositories

TZ = config.TZ
NOW = datetime(2026, 10, 21, 9, 30, tzinfo=TZ)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(notification_log, "STATE_PATH", tmp_path / "log.json")
    notification_log.reset_for_tests()
    monkeypatch.setattr(config, "TENANT", dataclasses.replace(
        config.TENANT, daily_briefing_enabled=True, ops_briefing_enabled=True, publish_confirm_digest_weekday=0))
    yield
    notification_log.reset_for_tests()


def task(page_id, status="進行中", *, artists=("a1",), **kw):
    return repositories.TaskItem(
        page_id=page_id, name=f"任務{page_id}", status=status, task_type=None,
        artist_page_ids=list(artists), discord_visible=True,
        end=kw.pop("end", datetime(2026, 10, 20, tzinfo=TZ)), url=f"https://www.notion.so/{page_id}", **kw,
    )


ARTISTS = {
    "a1": repositories.Artist(page_id="a1", name="方晨", discord_user_id="1"),
    "a2": repositories.Artist(page_id="a2", name="SOMA", discord_user_id="2"),
}


def test_時間到才發_同一天只發一次():
    assert not ops_briefing.briefing_due(NOW.replace(minute=29), None, 9, 30)
    assert ops_briefing.briefing_due(NOW, None, 9, 30)
    assert not ops_briefing.briefing_due(NOW, "2026-10-21", 9, 30)


def test_沒事時一行():
    text = ops_briefing.briefing_text(date(2026, 10, 21), ops_briefing.OpsBriefing())
    assert "10/21（三）營運早報" in text and "今天沒有需要注意的事" in text


@pytest.fixture
def repos(monkeypatch):
    data = SimpleNamespace(tasks=[], submissions=[], people=[], done=[])

    async def get_active_tasks():
        return data.tasks

    async def get_artist_map():
        return ARTISTS

    async def get_submissions_by_review_status(statuses):
        return data.submissions

    async def get_people_by_ids(ids):
        return data.people

    async def get_tasks_by_status(statuses):
        return data.done

    monkeypatch.setattr(repositories, "get_active_tasks", get_active_tasks)
    monkeypatch.setattr(repositories, "get_artist_map", get_artist_map)
    monkeypatch.setattr(repositories, "get_submissions_by_review_status", get_submissions_by_review_status)
    monkeypatch.setattr(repositories, "get_people_by_ids", get_people_by_ids)
    monkeypatch.setattr(repositories, "get_tasks_by_status", get_tasks_by_status)
    monkeypatch.setattr(ops_cog.time_collection, "active_collections", lambda: [])
    return data


def test_收集各區(repos):
    state = notification_log.shared()
    repos.tasks = [
        task("new", "已逾期"), task("old1", "已逾期"), task("old2", "已逾期", artists=("a1", "a2")),
        task("fix", "需修改", revision_deadline=NOW - timedelta(hours=2)),
        task("fix_ok", "需修改", revision_deadline=NOW + timedelta(days=1)),
        task("silent", "尚未開始", discord_notified_at=NOW - timedelta(days=4)),
        task("fresh", "尚未開始", discord_notified_at=NOW - timedelta(days=1)),
        task("review", "待審核"),
    ]
    repos.submissions = [repositories.SubmissionItem(
        page_id="s1", name="提交", status="待審核", task_page_ids=["review"], created_at=NOW - timedelta(hours=30))]
    ops_briefing.record_new_overdue(state, "new", NOW)
    ops_briefing.record_unread(state, "20261020", "SOMA")
    ops_briefing.add_teacher_note(state, "Tony｜歌唱B｜老師聯絡不到")

    b = asyncio.run(ops_cog.collect(NOW, state))

    assert [line.task_id for line in b.new_overdue] == ["new"]
    assert b.still_overdue_counts == {"方晨": 2, "SOMA": 1}
    assert [line.task_id for line in b.revision_passed] == ["fix"]
    assert [line.task_id for line in b.review_unassigned] == ["review"] and "30 小時" in b.review_unassigned[0].text
    assert [line.task_id for line in b.unreceived] == ["silent"]
    assert b.unread_artists == ["SOMA"]
    assert b.teacher_notes == ["Tony｜歌唱B｜老師聯絡不到"]
    assert b.publish_waiting == [], "10/21 是週三，不列發文待確認"

    text = ops_briefing.briefing_text(NOW.date(), b)
    for title in ("新逾期", "仍逾期", "方晨 2", "修改期限已過", "沒人負責", "還沒按收到", "昨天的早報沒看", "老師的課後作業"):
        assert title in text


def test_有負責審核人時不列(repos):
    repos.tasks = [task("review", "待審核", reviewer_page_ids=["p1"])]
    repos.submissions = [repositories.SubmissionItem(
        page_id="s1", name="提交", status="待審核", task_page_ids=["review"], created_at=NOW - timedelta(hours=30))]
    repos.people = [repositories.PersonItem(page_id="p1", name="甲", discord_user_id="9", identity_type="公司成員")]

    b = asyncio.run(ops_cog.collect(NOW, notification_log.shared()))

    assert b.review_unassigned == []


def test_週一列發文待確認(repos, monkeypatch):
    monday = NOW - timedelta(days=2)
    repos.done = [task("post", "已完成")]
    monkeypatch.setattr(repositories, "tasks_awaiting_publish_confirmation", lambda tasks, now, days: tasks)

    b = asyncio.run(ops_cog.collect(monday, notification_log.shared()))

    assert [line.task_id for line in b.publish_waiting] == ["post"]


class FakeChannel:
    def __init__(self):
        self.id = 77
        self.sent = []

    async def send(self, content=None, view=None):
        self.sent.append((content, view))
        return SimpleNamespace(id=500 + len(self.sent), channel=SimpleNamespace(id=77))


class FakeClient:
    def __init__(self, channel):
        self.channel, self.deleted = channel, []
        client = self

        class Partial:
            def get_partial_message(self, mid):
                class PM:
                    async def delete(self_inner):
                        client.deleted.append(mid)

                return PM()

        self.partial = Partial()

    def get_channel(self, _id):
        return self.channel

    def get_partial_messageable(self, _cid):
        return self.partial


def test_發送_刪昨天的_清掉佇列(repos):
    repos.tasks = [task("new", "已逾期")]
    state = notification_log.shared()
    ops_briefing.record_new_overdue(state, "new", NOW)
    state["ops_briefing"] = {"day": "20261020", "channel_id": 77, "message_id": 400}
    channel = FakeChannel()
    client = FakeClient(channel)

    assert asyncio.run(ops_cog.send_ops_briefing(client, NOW))

    content, view = channel.sent[0]
    assert "新逾期" in content
    assert [c.custom_id for c in view.children] == ["ops_detail:20261021"]
    assert client.deleted == [400]
    assert ops_briefing.new_overdue_ids(state) == set()


def test_看詳細附_Notion_連結(monkeypatch):
    target = task("new", "已逾期")

    async def get_task(_id):
        return target

    async def get_artist_map():
        return ARTISTS

    monkeypatch.setattr(repositories, "get_task", get_task)
    monkeypatch.setattr(repositories, "get_artist_map", get_artist_map)
    edits = []

    async def defer(**kwargs):
        return None

    async def edit_original_response(**kwargs):
        edits.append(kwargs)

    interaction = SimpleNamespace(data={"values": ["new"]}, response=SimpleNamespace(defer=defer),
                                  edit_original_response=edit_original_response)
    select = ops_cog.OpsDetailSelect("20261021", [discord.SelectOption(label="x", value="new")])
    asyncio.run(select.callback(interaction))

    assert "https://www.notion.so/new" in edits[0]["embed"].description
    assert "方晨" in edits[0]["embed"].description


def test_逾期不再即時一筆一則_改記進早報(monkeypatch):
    state = notification_log.shared()
    state["reminders_baseline_done"] = True
    sent, updates = [], []

    class Channel:
        async def send(self, content=None, **kw):
            sent.append(content)

    class Bot:
        def get_channel(self, _id):
            return Channel()

        async def fetch_user(self, _id):
            raise AssertionError("早報模式不私訊藝人")

    async def get_active_tasks():
        return [task("t1", end=datetime(2026, 10, 1, tzinfo=TZ))]

    async def get_artist_map():
        return ARTISTS

    async def update(page_id, status):
        updates.append((page_id, status))

    async def noop(*a, **k):
        return True

    monkeypatch.setattr(repositories, "get_active_tasks", get_active_tasks)
    monkeypatch.setattr(repositories, "get_artist_map", get_artist_map)
    monkeypatch.setattr(repositories, "update_task_status", update)
    monkeypatch.setattr(repositories, "append_reminder_ack_line", noop)
    monkeypatch.setattr(audit, "log_event", noop)
    monkeypatch.setattr(tasks_artist, "reminder_view", lambda *a: None)
    monkeypatch.setattr(reminders_cog, "datetime", SimpleNamespace(now=lambda tz=None: NOW))
    cog = SimpleNamespace(bot=Bot(), _sent={})
    cog._once = lambda rid, stage: reminders_cog.RemindersCog._once(cog, rid, stage)

    asyncio.run(reminders_cog.RemindersCog._check_task_reminders(cog))

    assert updates == [("t1", "已逾期")]
    assert sent == []
    assert ops_briefing.new_overdue_ids(state) == {"t1"}


def test_昨天早報沒按看到_記進營運早報(monkeypatch):
    state = notification_log.shared()
    state["briefings"] = {"1": {"day": "20261020", "channel_id": 9, "message_id": 8, "task_ids": ["x"], "artist_name": "方晨"}}

    async def get_active_tasks():
        return []

    async def get_artist_map():
        return {"a1": ARTISTS["a1"]}

    class Client:
        def get_partial_messageable(self, _cid):
            class P:
                def get_partial_message(self, _mid):
                    class M:
                        async def delete(self_inner):
                            return None

                    return M()

            return P()

    monkeypatch.setattr(repositories, "get_active_tasks", get_active_tasks)
    monkeypatch.setattr(repositories, "get_artist_map", get_artist_map)
    asyncio.run(briefing.send_daily_briefings(Client(), NOW.replace(hour=9, minute=0)))

    assert ops_briefing.unread_for(state, "20261020") == ["方晨"]


def test_只派給同仁的任務不算藝人還沒按收到(repos):
    repos.tasks = [task("staff_only", "尚未開始", artists=(), discord_notified_at=NOW - timedelta(days=5))]

    b = asyncio.run(ops_cog.collect(NOW, notification_log.shared()))

    assert b.unreceived == []
