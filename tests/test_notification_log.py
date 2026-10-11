"""M3-23：任務通知去重寫檔、按「收到任務」後把舊通知改成已收到。"""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import config
from cogs import reminders as reminders_cog
from cogs import tasks_artist
from services import audit, notification_log, repositories

TZ = config.TZ
NOW = datetime(2026, 10, 11, 10, 0, tzinfo=TZ)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(notification_log, "STATE_PATH", tmp_path / "log.json")
    notification_log.reset_for_tests()
    yield
    notification_log.reset_for_tests()


# ---------- 紀錄本身 ----------

class TestStore:
    def test_提醒紀錄的key含期限日_延期後會重新觸發(self):
        state = notification_log.load()
        d1 = datetime(2026, 10, 15, tzinfo=TZ)
        notification_log.mark_reminder_sent(state, "t1", "3 天前", d1, NOW)
        assert notification_log.reminder_sent(state, "t1", "3 天前", d1)
        assert not notification_log.reminder_sent(state, "t1", "3 天前", d1 + timedelta(days=3))
        assert not notification_log.reminder_sent(state, "t1", "1 天前", d1)

    def test_寫檔後重新讀取仍在_模擬重啟(self):
        state = notification_log.shared()
        notification_log.mark_reminder_sent(state, "t1", "3 天前", None, NOW)
        notification_log.persist()
        notification_log.reset_for_tests()
        assert notification_log.reminder_sent(notification_log.shared(), "t1", "3 天前", None)

    def test_壞掉的檔案當成空的不炸(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        assert notification_log.load(bad) == {"reminders": {}, "messages": {}}

    def test_訊息紀錄依任務與收件人分開_清除只清那一位(self):
        state = notification_log.load()
        notification_log.record_message(state, "t1", 100, 9, 1, NOW)
        notification_log.record_message(state, "t1", 100, 9, 2, NOW)
        notification_log.record_message(state, "t1", 200, 8, 3, NOW)
        notification_log.record_message(state, "t2", 100, 9, 4, NOW)
        assert [m["message_id"] for m in notification_log.messages_for(state, "t1", 100)] == [1, 2]
        notification_log.forget_messages(state, "t1", 100)
        assert notification_log.messages_for(state, "t1", 100) == []
        assert len(notification_log.messages_for(state, "t1", 200)) == 1
        assert len(notification_log.messages_for(state, "t2", 100)) == 1

    def test_過期紀錄會被清掉(self):
        state = notification_log.load()
        old = NOW - timedelta(days=60)
        notification_log.mark_reminder_sent(state, "old", "3 天前", None, old)
        notification_log.mark_reminder_sent(state, "new", "3 天前", None, NOW)
        notification_log.record_message(state, "old", 1, 1, 1, old)
        notification_log.prune(state, NOW)
        assert not notification_log.reminder_sent(state, "old", "3 天前", None)
        assert notification_log.reminder_sent(state, "new", "3 天前", None)
        assert state["messages"] == {}


# ---------- 按「收到任務」後清舊通知 ----------

class FakeMessage:
    def __init__(self, channel_id, message_id, content="任務通知內容"):
        self.channel = SimpleNamespace(id=channel_id)
        self.id = message_id
        self.content = content
        self.edits = []

    async def edit(self, content=None, view="unchanged"):
        self.edits.append({"content": content, "view": view})
        self.content = content


class FakeClient:
    def __init__(self, messages, missing=()):
        self.messages = {m.id: m for m in messages}
        self.missing = set(missing)

    def get_partial_messageable(self, channel_id):
        client = self

        class Partial:
            async def fetch_message(self, message_id):
                if message_id in client.missing:
                    raise RuntimeError("Unknown Message")
                return client.messages[message_id]

        return Partial()


class TestMarkReceived:
    def test_同一任務的歷史通知全部改成已收到並拿掉按鈕(self):
        old1, old2, other_task = FakeMessage(9, 1), FakeMessage(9, 2), FakeMessage(9, 3)
        state = notification_log.shared()
        for m, task_id in ((old1, "t1"), (old2, "t1"), (other_task, "t2")):
            notification_log.record_message(state, task_id, 100, 9, m.id, NOW)
        client = FakeClient([old1, old2, other_task])

        edited = asyncio.run(tasks_artist.mark_previous_notifications_received(client, "t1", 100, NOW))

        assert edited == 2
        for m in (old1, old2):
            assert m.edits[-1]["view"] is None
            assert "✅ 已收到（10/11 10:00）" in m.content
        assert other_task.edits == []  # 別的任務不受影響
        assert notification_log.messages_for(state, "t1", 100) == []

    def test_按的那一則就算沒被紀錄也會處理(self):
        current = FakeMessage(9, 7)
        client = FakeClient([current])
        edited = asyncio.run(
            tasks_artist.mark_previous_notifications_received(client, "t1", 100, NOW, current)
        )
        assert edited == 1 and current.edits[-1]["view"] is None

    def test_別的藝人的通知不動(self):
        mine, theirs = FakeMessage(9, 1), FakeMessage(8, 2)
        state = notification_log.shared()
        notification_log.record_message(state, "t1", 100, 9, 1, NOW)
        notification_log.record_message(state, "t1", 200, 8, 2, NOW)
        asyncio.run(tasks_artist.mark_previous_notifications_received(
            FakeClient([mine, theirs]), "t1", 100, NOW))
        assert theirs.edits == []
        assert len(notification_log.messages_for(state, "t1", 200)) == 1

    def test_被刪掉的舊通知略過_不讓流程失敗(self):
        alive = FakeMessage(9, 2)
        state = notification_log.shared()
        notification_log.record_message(state, "t1", 100, 9, 1, NOW)
        notification_log.record_message(state, "t1", 100, 9, 2, NOW)
        edited = asyncio.run(tasks_artist.mark_previous_notifications_received(
            FakeClient([alive], missing={1}), "t1", 100, NOW))
        assert edited == 1

    def test_重複按不會重複加上已收到字樣(self):
        msg = FakeMessage(9, 1)
        client = FakeClient([msg])
        asyncio.run(tasks_artist.mark_previous_notifications_received(client, "t1", 100, NOW, msg))
        later = NOW + timedelta(hours=2)
        asyncio.run(tasks_artist.mark_previous_notifications_received(client, "t1", 100, later, msg))
        assert msg.content.count("✅ 已收到") == 1


class TestSendTaskDm:
    def test_送出時記下訊息編號(self):
        sent = FakeMessage(55, 77)

        class User:
            id = 100

            async def send(self, content, view=None):
                sent.content = content
                return sent

        asyncio.run(tasks_artist.send_task_dm(User(), "t1", "內容"))
        records = notification_log.messages_for(notification_log.shared(), "t1", 100)
        assert [(r["channel_id"], r["message_id"]) for r in records] == [(55, 77)]


# ---------- 截止前提醒：重啟不重發 ----------

def reminder_task(page_id="t1", end=datetime(2026, 10, 13, tzinfo=TZ), status="進行中"):
    return repositories.TaskItem(
        page_id=page_id, name="任務", status=status, task_type=None,
        artist_page_ids=["a1"], discord_visible=True, end=end,
    )


class Harness:
    """不啟動 discord loop，直接呼叫 RemindersCog 的提醒方法。"""

    def __init__(self, monkeypatch, tasks):
        self.dms = []
        self.status_updates = []
        harness = self

        class User:
            async def send(self, content, view=None):
                harness.dms.append(content)

        class Bot:
            async def fetch_user(self, _):
                return User()

            def get_channel(self, _):
                return None

        async def get_active_tasks():
            return tasks

        async def get_artist_map():
            return {"a1": repositories.Artist(page_id="a1", name="藝人", discord_user_id="1")}

        async def noop(*args, **kwargs):
            return True

        async def update_status(page_id, status):
            harness.status_updates.append((page_id, status))

        monkeypatch.setattr(repositories, "get_active_tasks", get_active_tasks)
        monkeypatch.setattr(repositories, "get_artist_map", get_artist_map)
        monkeypatch.setattr(repositories, "append_reminder_ack_line", noop)
        monkeypatch.setattr(repositories, "update_task_status", update_status)
        monkeypatch.setattr(audit, "log_event", noop)
        monkeypatch.setattr(tasks_artist, "reminder_view", lambda *a: None)
        monkeypatch.setattr(reminders_cog, "datetime", SimpleNamespace(now=lambda tz=None: NOW))
        self.bot = Bot()

    def run_once(self):
        """每次都是新的 cog（等同 bot 重啟後記憶體全空）。"""
        cog = SimpleNamespace(bot=self.bot, _sent={})
        cog._once = lambda rid, stage: reminders_cog.RemindersCog._once(cog, rid, stage)
        asyncio.run(reminders_cog.RemindersCog._check_task_reminders(cog))


class TestReminderDedupSurvivesRestart:
    def test_第一次啟用只建立基準不發送(self, monkeypatch):
        h = Harness(monkeypatch, [reminder_task()])
        h.run_once()
        assert h.dms == []
        h.run_once()
        assert h.dms == []

    def test_基準之後新進窗口的提醒只發一次_重啟也不重發(self, monkeypatch):
        state = notification_log.shared()
        state["reminders_baseline_done"] = True
        notification_log.persist()
        h = Harness(monkeypatch, [reminder_task()])  # 剩 2 天：D-5、D-3 都進窗口
        h.run_once()
        first = len(h.dms)
        assert first >= 1
        notification_log.reset_for_tests()  # 模擬重啟：記憶體全清，只剩檔案
        h.run_once()
        assert len(h.dms) == first

    def test_延期後新的期限會重新提醒(self, monkeypatch):
        state = notification_log.shared()
        state["reminders_baseline_done"] = True
        tasks = [reminder_task()]
        h = Harness(monkeypatch, tasks)
        h.run_once()
        first = len(h.dms)
        tasks[0] = reminder_task(end=datetime(2026, 10, 14, tzinfo=TZ))
        h.run_once()
        assert len(h.dms) > first

    def test_M2_13_改回進行中沒延期_同一個process內也會再升級(self, monkeypatch):
        """先前靠記憶體去重，同一個 process 內改回進行中就再也不通知，重啟後才會——不一致。"""
        state = notification_log.shared()
        state["reminders_baseline_done"] = True
        overdue = reminder_task(end=datetime(2026, 10, 1, tzinfo=TZ))
        h = Harness(monkeypatch, [overdue])
        cog = SimpleNamespace(bot=h.bot, _sent={})
        cog._once = lambda rid, stage: reminders_cog.RemindersCog._once(cog, rid, stage)
        asyncio.run(reminders_cog.RemindersCog._check_task_reminders(cog))
        assert h.status_updates == [("t1", "已逾期")]
        # 控方在 Notion 改回進行中（同一個 cog，記憶體還在）
        asyncio.run(reminders_cog.RemindersCog._check_task_reminders(cog))
        assert h.status_updates == [("t1", "已逾期"), ("t1", "已逾期")]
        assert len(h.dms) == 2

    def test_M2_13_寫入已逾期失敗時不會每輪重複通知(self, monkeypatch):
        state = notification_log.shared()
        state["reminders_baseline_done"] = True
        h = Harness(monkeypatch, [reminder_task(end=datetime(2026, 10, 1, tzinfo=TZ))])

        async def failing(page_id, status):
            raise RuntimeError("Notion 429")

        monkeypatch.setattr(repositories, "update_task_status", failing)
        cog = SimpleNamespace(bot=h.bot, _sent={})
        cog._once = lambda rid, stage: reminders_cog.RemindersCog._once(cog, rid, stage)
        asyncio.run(reminders_cog.RemindersCog._check_task_reminders(cog))
        asyncio.run(reminders_cog.RemindersCog._check_task_reminders(cog))
        assert len(h.dms) == 1

    def test_基準那一輪逾期任務照常升級(self, monkeypatch):
        overdue = reminder_task(end=datetime(2026, 10, 1, tzinfo=TZ))
        h = Harness(monkeypatch, [overdue])
        h.run_once()
        assert h.status_updates == [("t1", "已逾期")]
