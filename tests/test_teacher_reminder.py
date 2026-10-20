"""M5-01／M5-02：課後提醒老師到論壇發布。"""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import config
from cogs import channel_guard
from cogs import reminders as reminders_cog
from services import audit, notification_log, repositories, teacher_reminder
from services.repositories import Artist, PersonItem, TaskItem

TZ = config.TZ
CLASS_END = datetime(2026, 10, 11, 16, 0, tzinfo=TZ)
KW = dict(after_hours=2, daily_hour=10, max_days=7)


def course(status="進行中", end=CLASS_END, has_time=True, project_type=repositories.PROJECT_TYPE_COURSE_TRAINING):
    return TaskItem(page_id="c1", name="吉他課", status=status, task_type=None,
                    project_types=[project_type], artist_page_ids=["a1"], teacher_page_ids=["t1"],
                    end=end, end_has_time=has_time)


class TestNextAction:
    def test_非課程任務不催(self):
        t = course(project_type=repositories.PROJECT_TYPE_PUBLISHING)
        assert teacher_reminder.next_action(t, CLASS_END + timedelta(hours=3), None, **KW) is None

    def test_課後未滿門檻不催_滿了就發第一次(self):
        assert teacher_reminder.next_action(course(), CLASS_END + timedelta(hours=1), None, **KW) is None
        assert teacher_reminder.next_action(course(), CLASS_END + timedelta(hours=2), None, **KW) == "remind"

    def test_發過後同一天不再催_隔天過整點才再催(self):
        record = {"count": 1, "last_date": "2026-10-11"}
        assert teacher_reminder.next_action(course(), datetime(2026, 10, 11, 22, 0, tzinfo=TZ), record, **KW) is None
        assert teacher_reminder.next_action(course(), datetime(2026, 10, 12, 9, 0, tzinfo=TZ), record, **KW) is None
        assert teacher_reminder.next_action(course(), datetime(2026, 10, 12, 10, 0, tzinfo=TZ), record, **KW) == "remind"

    def test_催滿天數改通知控方_之後不再動作(self):
        now = datetime(2026, 10, 19, 10, 0, tzinfo=TZ)
        assert teacher_reminder.next_action(course(), now, {"count": 7, "last_date": "2026-10-18"}, **KW) == "escalate"
        assert teacher_reminder.next_action(course(), now, {"count": 7, "escalated": True}, **KW) is None

    def test_控方結案就停(self):
        assert teacher_reminder.next_action(course(status="已完成"), CLASS_END + timedelta(days=1), None, **KW) is None

    def test_很久以前的課不再開始催(self):
        assert teacher_reminder.next_action(course(), CLASS_END + timedelta(days=8), None, **KW) is None

    def test_只填日期的課當天晚上才算結束(self):
        t = course(end=datetime(2026, 10, 11, tzinfo=TZ), has_time=False)
        assert teacher_reminder.next_action(t, datetime(2026, 10, 11, 18, 0, tzinfo=TZ), None, **KW) is None
        assert teacher_reminder.next_action(t, datetime(2026, 10, 12, 2, 0, tzinfo=TZ), None, **KW) == "remind"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(notification_log, "STATE_PATH", tmp_path / "log.json")
    # 這裡測的是營運早報關閉時「直接發控方頻道」的行為；早報模式見 tests/test_ops_briefing.py
    import dataclasses
    import config as _config

    monkeypatch.setattr(_config, "TENANT", dataclasses.replace(_config.TENANT, ops_briefing_enabled=False))
    notification_log.reset_for_tests()
    yield
    notification_log.reset_for_tests()


class Harness:
    def __init__(self, monkeypatch, tasks, teachers, now):
        self.dms, self.admin = [], []
        h = self

        class User:
            async def send(self, content, view=None):
                h.dms.append(content)

        class Bot:
            async def fetch_user(self, uid):
                return User()

        async def active():
            return tasks

        async def artist_map():
            return {"a1": Artist(page_id="a1", name="辰葳", discord_user_id="1")}

        async def people(ids):
            return [p for p in teachers if p.page_id in ids]

        async def try_send(channel, **kw):
            h.admin.append(kw["content"])

        async def noop(*a, **k):
            return None

        monkeypatch.setattr(repositories, "get_active_tasks", active)
        monkeypatch.setattr(repositories, "get_artist_map", artist_map)
        monkeypatch.setattr(repositories, "get_people_items_by_ids", people)
        monkeypatch.setattr(channel_guard, "try_send", try_send)
        monkeypatch.setattr(audit, "log_event", noop)
        self.set_now(monkeypatch, now)
        self.bot = Bot()
        self.monkeypatch = monkeypatch

    def set_now(self, monkeypatch, now):
        monkeypatch.setattr(reminders_cog, "datetime", SimpleNamespace(now=lambda tz=None: now))

    def run(self):
        cog = SimpleNamespace(bot=self.bot, _admin_channel=lambda: "admin")
        asyncio.run(reminders_cog.RemindersCog._check_teacher_post_class(cog))


TEACHER = PersonItem(page_id="t1", name="Tony", discord_user_id="9", identity_type="老師")


class TestTeacherReminderCog:
    def test_課後私訊老師_含學生與上課時間(self, monkeypatch):
        h = Harness(monkeypatch, [course()], [TEACHER], CLASS_END + timedelta(hours=3))
        h.run()
        assert len(h.dms) == 1 and "辰葳" in h.dms[0] and "10/11 16:00" in h.dms[0]
        assert h.admin == []

    def test_同一天重跑或重啟都不重發(self, monkeypatch):
        h = Harness(monkeypatch, [course()], [TEACHER], CLASS_END + timedelta(hours=3))
        h.run()
        h.run()
        notification_log.reset_for_tests()
        h.run()
        assert len(h.dms) == 1

    def test_老師沒有Discord_ID就轉控方頻道(self, monkeypatch):
        no_id = PersonItem(page_id="t1", name="Tony", discord_user_id="", identity_type="老師")
        h = Harness(monkeypatch, [course()], [no_id], CLASS_END + timedelta(hours=3))
        h.run()
        assert h.dms == [] and len(h.admin) == 1 and "Tony" in h.admin[0]

    def test_催滿七天後只通知控方一次(self, monkeypatch):
        h = Harness(monkeypatch, [course()], [TEACHER], CLASS_END + timedelta(hours=3))
        h.run()
        for day in range(1, 9):
            h.set_now(monkeypatch, datetime(2026, 10, 11 + day, 10, 30, tzinfo=TZ))
            h.run()
        assert len(h.dms) == 7
        assert len(h.admin) == 1 and "未結案" in h.admin[0]

    def test_同一堂課每位學生各一筆_老師只收一則(self, monkeypatch):
        # Notion 上同一堂課是每位學生各建一筆（10/10 製作課有 5 筆），不合併老師會收到 5 則。
        tasks = []
        for i, aid in enumerate(["a1", "a2"]):
            t = course()
            t.page_id, t.artist_page_ids = f"c{i}", [aid]
            tasks.append(t)
        h = Harness(monkeypatch, tasks, [TEACHER], CLASS_END + timedelta(hours=3))

        async def artist_map():
            return {"a1": Artist(page_id="a1", name="辰葳", discord_user_id="1"),
                    "a2": Artist(page_id="a2", name="SOMA", discord_user_id="2")}

        monkeypatch.setattr(repositories, "get_artist_map", artist_map)
        h.run()
        assert len(h.dms) == 1 and "辰葳" in h.dms[0] and "SOMA" in h.dms[0]
        h.run()
        assert len(h.dms) == 1  # 兩筆都記為已提醒


class TestTeacherDigest:
    """M3-25 第三階段：同一位老師這一輪有好幾堂課，合成一則（原本一堂課一則）。"""

    def test_兩堂課只收一則_都列出來(self, monkeypatch):
        import dataclasses

        guitar = course()
        singing = dataclasses.replace(course(), page_id="c2", name="歌唱課",
                                      end=CLASS_END - timedelta(hours=3))
        h = Harness(monkeypatch, [guitar, singing], [TEACHER], CLASS_END + timedelta(hours=3))
        h.run()

        assert len(h.dms) == 1
        assert "吉他課" in h.dms[0] and "歌唱課" in h.dms[0]
        assert h.admin == []

    def test_不同老師各自一則(self, monkeypatch):
        import dataclasses

        other = PersonItem(page_id="t2", name="Amy", discord_user_id="8", identity_type="老師")
        h = Harness(monkeypatch, [course(), dataclasses.replace(course(), page_id="c2", name="歌唱課", teacher_page_ids=["t2"])],
                    [TEACHER, other], CLASS_END + timedelta(hours=3))
        h.run()

        assert len(h.dms) == 2
