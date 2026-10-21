"""M3-27：同一輪多筆任務合併成一則通知、重送清舊卡、收到後仍可回報問題。"""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

import config
from cogs import tasks_artist
from services import audit, notification_log, repositories, task_bundle

TZ = config.TZ


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(notification_log, "STATE_PATH", tmp_path / "log.json")
    notification_log.reset_for_tests()

    async def noop(*a, **k):
        return True

    monkeypatch.setattr(audit, "log_event", noop)
    monkeypatch.setattr(repositories, "mark_task_discord_notified", noop)
    yield
    notification_log.reset_for_tests()


def task(page_id, *, artists=("a1",), staff=(), status="尚未開始", name=None):
    return repositories.TaskItem(
        page_id=page_id, name=name or f"任務{page_id}", status=status, task_type="行政",
        artist_page_ids=list(artists), staff_page_ids=list(staff), discord_visible=True,
        end=datetime(2026, 10, 28, tzinfo=TZ), end_has_time=False,
    )


ARTISTS = {
    "a1": repositories.Artist(page_id="a1", name="Johnny（測試）", discord_user_id="100"),
    "a2": repositories.Artist(page_id="a2", name="方晨", discord_user_id="200"),
}
PEOPLE = {"p1": SimpleNamespace(page_id="p1", name="Johnny", discord_user_id="300")}


class FakeMessage:
    def __init__(self, mid, content, view):
        self.id, self.content, self.view = mid, content, view
        self.channel = SimpleNamespace(id=9)


class FakeBot:
    def __init__(self, fail_users=()):
        self.sent: list[tuple[str, str, object]] = []  # (user_id, content, view)
        self.deleted: list[int] = []
        self.fail_users = set(fail_users)
        bot = self

        class User:
            def __init__(self, uid):
                self.id = int(uid)

            async def send(self, content, view=None):
                if str(self.id) in bot.fail_users:
                    raise RuntimeError("cannot DM")
                bot.sent.append((str(self.id), content, view))
                return FakeMessage(1000 + len(bot.sent), content, view)

        self.User = User

    async def fetch_user(self, uid):
        return self.User(uid)

    def get_channel(self, _):
        return None

    def get_partial_messageable(self, _channel_id):
        bot = self

        class Partial:
            def get_partial_message(self, message_id):
                class PM:
                    async def delete(self_inner):
                        bot.deleted.append(message_id)

                return PM()

        return Partial()


@pytest.fixture
def repos(monkeypatch):
    async def artists_by_ids(ids):
        return [ARTISTS[i] for i in ids if i in ARTISTS]

    async def people_by_ids(ids):
        return [PEOPLE[i] for i in ids if i in PEOPLE]

    monkeypatch.setattr(repositories, "get_artists_by_ids", artists_by_ids)
    monkeypatch.setattr(repositories, "get_people_items_by_ids", people_by_ids)


def ids(view):
    return [child.custom_id for child in view.children] if view else []


def send(bot, tasks, kind=task_bundle.KIND_DISPATCH):
    return asyncio.run(tasks_artist.send_task_notifications(bot, tasks, kind=kind))


def test_同一位藝人兩筆_合併成一則(repos):
    bot = FakeBot()
    outcomes = send(bot, [task("t1"), task("t2")])
    assert len(bot.sent) == 1
    user, content, view = bot.sent[0]
    assert user == "100" and "任務通知**（2）" in content and "任務t1" in content and "任務t2" in content
    assert [i.split(":")[0] for i in ids(view)] == ["bundle_detail", "bundle_receive", "bundle_concern"]
    assert all(o.is_complete for o in outcomes.values())


def test_只有一筆_維持原本的任務卡(repos):
    bot = FakeBot()
    send(bot, [task("t1")])
    assert len(bot.sent) == 1
    assert ids(bot.sent[0][2]) == ["task_receipt:t1", "task_concern:t1"]


def test_不同藝人各自分開(repos):
    bot = FakeBot()
    send(bot, [task("t1", artists=("a1",)), task("t2", artists=("a2",))])
    assert sorted(u for u, _, _ in bot.sent) == ["100", "200"]
    assert all(ids(v)[0].startswith("task_receipt") for _, _, v in bot.sent)


def test_重送時舊卡先刪掉(repos):
    bot = FakeBot()
    send(bot, [task("t1")])
    first = 1001
    send(bot, [task("t1"), task("t2")])
    assert first in bot.deleted
    assert "任務通知**（2）" in bot.sent[-1][1]


def test_舊合併訊息還有別的任務沒重送_先保留(repos):
    bot = FakeBot()
    send(bot, [task("t1"), task("t2")])
    bundle_msg = 1001
    send(bot, [task("t1")])  # 只重送 t1：舊合併訊息裡還有 t2，要留著
    assert bundle_msg not in bot.deleted
    send(bot, [task("t2")])  # t2 也重送了：舊合併訊息已經沒有要留的任務，刪掉
    assert bundle_msg in bot.deleted


def test_相關同仁也合併成一則清單_沒有按鈕(repos):
    bot = FakeBot()
    send(bot, [task("t1", artists=(), staff=("p1",)), task("t2", staff=("p1",))])
    staff_msgs = [(c, v) for u, c, v in bot.sent if u == "300"]
    assert len(staff_msgs) == 1
    content, view = staff_msgs[0]
    assert "同仁知會（2）" in content and view is None and "請先按" not in content


def test_課後作業通知也合併_不發同仁(repos):
    bot = FakeBot()
    send(bot, [task("t1", staff=("p1",)), task("t2")], kind=task_bundle.KIND_ASSIGNMENT)
    assert len(bot.sent) == 1
    assert "課後作業通知**（2）" in bot.sent[0][1]


def test_有人送不到_只影響那個人(repos):
    bot = FakeBot(fail_users={"200"})
    outcomes = send(bot, [task("t1", artists=("a1", "a2"))])
    assert outcomes["t1"].sent == 1 and not outcomes["t1"].is_complete


def test_沒有任何收件人回_None(repos):
    assert send(FakeBot(), [task("t1", artists=())])["t1"] is None


# ---------- 全部收到 ----------

def test_全部收到_每筆都記收到_收成一行留有問題(repos, monkeypatch):
    bot = FakeBot()
    send(bot, [task("t1"), task("t2")])
    _, content, view = bot.sent[0]
    bundle_id = ids(view)[0].split(":")[1]
    tasks = {"t1": task("t1"), "t2": task("t2")}
    acked, edits = [], []

    async def brief(tid):
        return tasks[tid]

    async def find_artist(_uid):
        return ARTISTS["a1"]

    async def ack(tid, at):
        acked.append(tid)
        return True

    monkeypatch.setattr(repositories, "get_task_brief", brief)
    monkeypatch.setattr(repositories, "find_artist_by_discord_id", find_artist)
    monkeypatch.setattr(repositories, "acknowledge_task", ack)

    async def defer():
        return None

    async def edit(**kw):
        edits.append(kw)

    message = SimpleNamespace(id=1001, channel=SimpleNamespace(id=9), components=[], edit=edit)
    interaction = SimpleNamespace(user=SimpleNamespace(id=100), client=bot, message=message,
                                  response=SimpleNamespace(defer=defer), followup=SimpleNamespace(send=None))
    asyncio.run(tasks_artist.BundleReceiveButton(bundle_id).callback(interaction))

    assert acked == ["t1", "t2"]
    assert "已收到 2 筆任務" in edits[0]["content"]
    assert ids(edits[0]["view"]) == [f"bundle_concern:{bundle_id}"]
    assert 1001 not in bot.deleted, "按的那則收成一行，不刪"


# ---------- 回報問題 ----------

def _concern_interaction():
    calls = {}

    async def send_message(content, **kw):
        calls["message"] = content

    async def send_modal(modal):
        calls["modal"] = modal

    return SimpleNamespace(response=SimpleNamespace(send_message=send_message, send_modal=send_modal)), calls


@pytest.mark.parametrize("status,allowed", [("尚未開始", True), ("進行中", True), ("待提交", True),
                                            ("待審核", False), ("需修改", False)])
def test_已交件或修改中不能用按鈕回報(monkeypatch, status, allowed):
    async def brief(tid):
        return task(tid, status=status)

    monkeypatch.setattr(repositories, "get_task_brief", brief)
    interaction, calls = _concern_interaction()
    asyncio.run(tasks_artist.open_concern_modal(interaction, "t1"))
    assert ("modal" in calls) == allowed
    if not allowed:
        assert calls["message"] == task_bundle.CONCERN_BLOCKED_REPLY


def test_合併文案與看詳細拿掉單筆卡的按鈕提示():
    text = task_bundle.bundle_text(task_bundle.KIND_DISPATCH, [task("t1"), task("t2")], lambda t: "截止 10/28")
    assert text.startswith("📌 **任務通知**（2）") and "全部收到" in text
    from services import task_notifications

    detail = task_notifications.strip_artist_prompts(task_notifications.notification_message(task("t1")))
    assert "請先按" not in detail and "任務t1" in detail
