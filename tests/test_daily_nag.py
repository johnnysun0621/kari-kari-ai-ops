"""M3-11：每日催辦。"""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import config
from cogs import channel_guard
from cogs import reminders as reminders_cog
from services import audit, daily_nag, notification_log, repositories
from services.repositories import Artist, SubmissionItem, TaskItem

TZ = config.TZ
NOW = datetime(2026, 10, 12, 10, 5, tzinfo=TZ)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(notification_log, "STATE_PATH", tmp_path / "log.json")
    # 這裡測的是逐則通知（每日早報關掉時的行為，M3-24 之前的預設）；早報模式見 tests/test_daily_briefing.py
    import dataclasses
    import config as _config

    monkeypatch.setattr(_config, "TENANT", dataclasses.replace(_config.TENANT, daily_briefing_enabled=False))
    notification_log.reset_for_tests()
    yield
    notification_log.reset_for_tests()


def task(page_id, status, artists=("a1",), visible=True, reviewers=(), **kw):
    return TaskItem(
        page_id=page_id, name=f"任務{page_id}", status=status, task_type=None,
        artist_page_ids=list(artists), reviewer_page_ids=list(reviewers),
        discord_visible=visible, end=datetime(2026, 10, 5, tzinfo=TZ), **kw,
    )


ARTISTS = {
    "a1": Artist(page_id="a1", name="藝人一", discord_user_id="1"),
    "a2": Artist(page_id="a2", name="藝人二", discord_user_id="2"),
    "a3": Artist(page_id="a3", name="沒ID", discord_user_id=""),
}


class TestSchedule:
    def test_還沒到整點不跑(self):
        assert not daily_nag.daily_nag_due(NOW.replace(hour=9), None, 10)

    def test_過了整點且今天沒跑過就跑(self):
        assert daily_nag.daily_nag_due(NOW, "2026-10-11", 10)

    def test_今天跑過就不再跑(self):
        assert not daily_nag.daily_nag_due(NOW, "2026-10-12", 10)


class TestGrouping:
    def test_只收已逾期與需修改且已派發的任務(self):
        tasks = [
            task("1", "已逾期"), task("2", "需修改"), task("3", "進行中"),
            task("4", "待審核"), task("5", "已逾期", visible=False),
        ]
        [nag] = daily_nag.group_artist_nags(tasks, ARTISTS)
        assert [t.page_id for t in nag.overdue] == ["1"]
        assert [t.page_id for t in nag.revision] == ["2"]

    def test_多藝人任務兩位都收到_沒Discord_ID的略過(self):
        nags = daily_nag.group_artist_nags([task("1", "已逾期", artists=("a1", "a2", "a3"))], ARTISTS)
        assert sorted(n.artist.page_id for n in nags) == ["a1", "a2"]

    def test_訊息含任務與期限_不含網址(self):
        nag = daily_nag.ArtistNag(
            artist=ARTISTS["a1"],
            overdue=[task("1", "已逾期")],
            revision=[task("2", "需修改", revision_deadline=datetime(2026, 10, 13, 18, 0, tzinfo=TZ))],
        )
        message = daily_nag.artist_nag_message(nag)
        assert "任務1" in message and "截止 10/05" in message
        assert "任務2" in message and "修改期限 10/13 18:00" in message
        assert "http" not in message


class TestPendingReview:
    def test_未過門檻或任務已結案的不列入(self):
        old = SubmissionItem(page_id="s1", name="交件A", status="待審核",
                             task_page_ids=["t1"], created_at=NOW - timedelta(hours=30))
        fresh = SubmissionItem(page_id="s2", name="交件B", status="待審核",
                               task_page_ids=["t1"], created_at=NOW - timedelta(hours=2))
        orphan = SubmissionItem(page_id="s3", name="交件C", status="待審核",
                                task_page_ids=["gone"], created_at=NOW - timedelta(hours=30))
        grouped = daily_nag.pending_review_tasks(
            [old, fresh, orphan], {"t1": task("t1", "待審核")}, NOW, 24, reminders_cog.review_reminder_due
        )
        assert {k: [s.page_id for s in v] for k, v in grouped.items()} == {"t1": ["s1"]}


class Harness:
    def __init__(self, monkeypatch, tasks, submissions=(), people=(), fail_users=()):
        self.dms: dict[str, list[str]] = {}
        self.admin: list[str] = []
        harness = self

        class User:
            def __init__(self, uid):
                self.uid = uid

            async def send(self, content, view=None):
                if self.uid in fail_users:
                    raise RuntimeError("cannot DM")
                harness.dms.setdefault(self.uid, []).append(content)

        class Bot:
            async def fetch_user(self, uid):
                return User(str(uid))

            def get_channel(self, _):
                return "admin"

        async def try_send(channel, **kwargs):
            harness.admin.append(kwargs["content"])
            return object()

        async def active():
            return tasks

        async def artist_map():
            return ARTISTS

        async def subs(statuses, **kw):
            return list(submissions)

        async def people_by_ids(ids):
            return [p for p in people if p.page_id in ids]

        async def noop(*a, **k):
            return None

        monkeypatch.setattr(repositories, "get_active_tasks", active)
        monkeypatch.setattr(repositories, "get_artist_map", artist_map)
        monkeypatch.setattr(repositories, "get_submissions_by_review_status", subs)
        monkeypatch.setattr(repositories, "get_people_by_ids", people_by_ids)
        monkeypatch.setattr(channel_guard, "try_send", try_send)
        monkeypatch.setattr(audit, "log_event", noop)
        monkeypatch.setattr(reminders_cog, "datetime", SimpleNamespace(now=lambda tz=None: NOW))
        self.bot = Bot()

    def cog(self):
        cog = SimpleNamespace(bot=self.bot, _sent={})
        cog._admin_channel = lambda: "admin"
        return cog

    def run(self, method="_check_daily_nags"):
        asyncio.run(getattr(reminders_cog.RemindersCog, method)(self.cog()))


def person(page_id, discord_id):
    return SimpleNamespace(page_id=page_id, name=page_id, discord_user_id=discord_id)


OLD_SUB = SubmissionItem(page_id="s1", name="交件A", status="待審核",
                         task_page_ids=["r1"], created_at=NOW - timedelta(hours=30))


class TestDailyNagCog:
    def test_一天只催一次_重啟後同一天也不重催(self, monkeypatch):
        h = Harness(monkeypatch, [task("1", "已逾期"), task("2", "需修改")])
        h.run()
        assert len(h.dms["1"]) == 1  # 兩筆任務彙整成一則
        h.run()
        notification_log.reset_for_tests()  # 模擬重啟
        h.run()
        assert len(h.dms["1"]) == 1

    def test_進行中的任務不催(self, monkeypatch):
        h = Harness(monkeypatch, [task("1", "進行中")])
        h.run()
        assert h.dms == {} and h.admin == []

    def test_待審核催指定審核人員_不發控方頻道(self, monkeypatch):
        h = Harness(monkeypatch, [task("r1", "待審核", reviewers=("p1",))],
                    submissions=[OLD_SUB], people=[person("p1", "9")])
        h.run()
        assert "交件A" in h.dms["9"][0]
        assert h.admin == []

    def test_沒指定審核人員就發控方頻道(self, monkeypatch):
        h = Harness(monkeypatch, [task("r1", "待審核")], submissions=[OLD_SUB])
        h.run()
        assert len(h.admin) == 1 and "交件A" in h.admin[0]

    def test_審核人員私訊失敗退回控方頻道(self, monkeypatch):
        h = Harness(monkeypatch, [task("r1", "待審核", reviewers=("p1",))],
                    submissions=[OLD_SUB], people=[person("p1", "9")], fail_users={"9"})
        h.run()
        assert len(h.admin) == 1

    def test_待審核不催藝人(self, monkeypatch):
        h = Harness(monkeypatch, [task("r1", "待審核", reviewers=("p1",))],
                    submissions=[OLD_SUB], people=[person("p1", "9")])
        h.run()
        assert "1" not in h.dms


class TestRevisionOverdueAlert:
    def test_修改期限逾時只通知控方一次_重啟也不重發(self, monkeypatch):
        overdue = task("1", "需修改", revision_deadline=NOW - timedelta(hours=1))
        h = Harness(monkeypatch, [overdue])
        h.run("_check_revision_deadlines")
        h.run("_check_revision_deadlines")
        notification_log.reset_for_tests()
        h.run("_check_revision_deadlines")
        assert len(h.admin) == 1 and "修改期限逾時" in h.admin[0]
        assert h.dms == {}  # 藝人端改由每日催辦處理
