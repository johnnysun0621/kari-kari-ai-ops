"""M6-05：公司同仁（相關同仁 → People）也走 Discord 收任務通知。"""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

import config
from cogs import tasks_artist
from services import audit, notification_log, repositories, task_notifications
from services.repositories import Artist, PersonItem, TaskItem

TZ = config.TZ


def task(artists=("a1",), staff=("p1",)):
    return TaskItem(
        page_id="t1", name="10/20 錄音", status="尚未開始", task_type="錄音",
        artist_page_ids=list(artists), staff_page_ids=list(staff), discord_visible=True,
        end=datetime(2026, 10, 20, 18, 0, tzinfo=TZ), end_has_time=True,
        description="請帶耳機，詳見 https://www.notion.so/secret",
    )


class FakeUser:
    def __init__(self, uid, fail=False):
        self.id = uid
        self.fail = fail
        self.sent = []

    async def send(self, content=None, view=None):
        if self.fail:
            raise RuntimeError("DM closed")
        self.sent.append((content, view))
        return SimpleNamespace(id=len(self.sent), channel=SimpleNamespace(id=9000 + self.id))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(notification_log, "STATE_PATH", tmp_path / "log.json")
    notification_log.reset_for_tests()
    users = {1: FakeUser(1), 2: FakeUser(2), 3: FakeUser(3, fail=True)}
    artists = {"a1": Artist(page_id="a1", name="辰葳", discord_user_id="1")}
    people = {
        "p1": PersonItem(page_id="p1", name="子喬", discord_user_id="2", identity_type="公司成員"),
        "p2": PersonItem(page_id="p2", name="沒有 ID", discord_user_id="", identity_type="公司成員"),
        "p3": PersonItem(page_id="p3", name="私訊關閉", discord_user_id="3", identity_type="公司成員"),
    }
    marked = []

    async def get_artists(ids):
        return [artists[i] for i in ids if i in artists]

    async def get_people(ids):
        return [people[i] for i in ids if i in people]

    async def mark(page_id):
        marked.append(page_id)

    async def log_event(**kw):
        return None

    monkeypatch.setattr(repositories, "get_artists_by_ids", get_artists)
    monkeypatch.setattr(repositories, "get_people_items_by_ids", get_people)
    monkeypatch.setattr(repositories, "mark_task_discord_notified", mark)
    monkeypatch.setattr(audit, "log_event", log_event)

    async def fetch_user(uid):
        return users[uid]

    bot = SimpleNamespace(fetch_user=fetch_user, get_channel=lambda _: None)
    yield SimpleNamespace(bot=bot, users=users, marked=marked)
    notification_log.reset_for_tests()


def run(env, t):
    return asyncio.run(tasks_artist.send_task_notification(env.bot, t))


def test_藝人與同仁都收到_同仁沒有按鈕(env):
    outcome = run(env, task())
    assert outcome.is_complete and outcome.sent == 1 and outcome.staff_sent == 1
    artist_msg, artist_view = env.users[1].sent[0]
    staff_msg, staff_view = env.users[2].sent[0]
    assert artist_view is not None  # 藝人有「收到任務」按鈕
    assert staff_view is None
    assert staff_msg.startswith(task_notifications.STAFF_HEADER)
    assert env.marked == ["t1"]


def test_同仁訊息不含Notion網址也沒有藝人交件指引(env):
    run(env, task())
    staff_msg = env.users[2].sent[0][0]
    assert "notion.so" not in staff_msg and "notion.site" not in staff_msg
    assert task_notifications.SUBMIT_GUIDE not in staff_msg
    assert "10/20 錄音" in staff_msg


def test_只掛同仁的任務也會通知(env):
    outcome = run(env, task(artists=()))
    assert outcome is not None and outcome.is_complete
    assert env.users[2].sent and not env.users[1].sent
    assert env.marked == ["t1"]


def test_同仁沒有DiscordID就略過_不影響藝人(env):
    outcome = run(env, task(staff=("p2",)))
    assert outcome.is_complete and outcome.staff_sent == 0
    assert env.users[1].sent


def test_同仁私訊失敗不讓藝人重送(env):
    # 有藝人時只看藝人：同仁失敗若讓整筆重試，藝人每 30 分鐘會重複收到同一則。
    outcome = run(env, task(staff=("p1", "p3")))
    assert outcome.is_complete and outcome.staff_sent == 1


def test_沒有任何可通知的人回None(env):
    assert run(env, task(artists=(), staff=("p2",))) is None


def test_相關同仁欄位不存在時讀成空清單():
    page = {
        "id": "t1", "properties": {
            "任務名稱": {"type": "title", "title": [{"plain_text": "x"}]},
            repositories.TASK_ARTIST_PROPERTY: {"type": "relation", "relation": [{"id": "a1"}]},
        },
    }
    item = repositories._to_task(page)
    assert item.artist_page_ids == ["a1"] and item.staff_page_ids == []


def test_相關同仁欄位有值會讀進來():
    page = {
        "id": "t1", "properties": {
            "任務名稱": {"type": "title", "title": [{"plain_text": "x"}]},
            repositories.TASK_STAFF_PROPERTY: {"type": "relation", "relation": [{"id": "p1"}]},
        },
    }
    assert repositories._to_task(page).staff_page_ids == ["p1"]
