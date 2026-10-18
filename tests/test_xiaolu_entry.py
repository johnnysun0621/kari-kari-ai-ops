"""小律入口改成按鈕（2026-10-18）：藝人在公開頻道發問時，bot 刪除前大家都看得到。

置頂訊息＋「💬 開始問小律」→ 直接開（或帶回）本人的私人討論串，問題不經過公開頻道。
"""
import asyncio
from types import SimpleNamespace

import discord
import pytest

from cogs import course_qa
from services import repositories


class FakeThread:
    def __init__(self, thread_id, archived=False, name="小律｜Johnny"):
        self.id = thread_id
        self.archived = archived
        self.name = name
        self.mention = f"<#{thread_id}>"
        self.sent: list[str] = []
        self.users: list[int] = []
        self.edits: list[dict] = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if "archived" in kwargs:
            self.archived = kwargs["archived"]
        if "name" in kwargs:
            self.name = kwargs["name"]

    async def add_user(self, user):
        self.users.append(user.id)

    async def send(self, content=None, **kwargs):
        self.sent.append(content)


class FakeParent:
    def __init__(self):
        self.created: list[FakeThread] = []

    async def create_thread(self, *, name, type, **kwargs):
        assert type == discord.ChannelType.private_thread, "一定要是私人討論串"
        thread = FakeThread(9000 + len(self.created), name=name)
        self.created.append(thread)
        return thread


@pytest.fixture
def env(monkeypatch, tmp_path):
    artist = repositories.Artist(page_id="a1", name="Johnny", discord_user_id="1")
    found = {"artist": artist}

    async def find_artist(_uid):
        return found["artist"]

    monkeypatch.setattr(repositories, "find_artist_by_discord_id", find_artist)
    monkeypatch.setattr(course_qa.discord, "TextChannel", FakeParent)
    monkeypatch.setattr(course_qa.discord, "Thread", FakeThread)
    threads: dict[int, FakeThread] = {}

    async def fetch_channel(thread_id):
        if thread_id in threads:
            return threads[thread_id]
        raise discord.NotFound(SimpleNamespace(status=404, reason="nf"), "nf")

    bot = SimpleNamespace(get_channel=lambda _id: None, fetch_channel=fetch_channel)
    cog = course_qa.CourseQACog.__new__(course_qa.CourseQACog)
    cog.bot = bot
    cog._thread_by_user = {}
    cog._save_thread_state = lambda: None
    parent = FakeParent()
    replies: list[str] = []

    async def defer(**kwargs):
        return None

    async def followup_send(content=None, **kwargs):
        replies.append(content)

    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1, name="johnny", display_name="Johnny", mention="<@1>"),
        channel=parent,
        response=SimpleNamespace(defer=defer),
        followup=SimpleNamespace(send=followup_send),
    )
    return SimpleNamespace(cog=cog, parent=parent, threads=threads, replies=replies,
                           interaction=interaction, found=found)


def press(env):
    asyncio.run(env.cog.open_private_thread(env.interaction))


def test_第一次按_開私人討論串並打招呼(env):
    press(env)

    thread = env.parent.created[0]
    assert thread.users == [1]
    assert "只有你看得到" in thread.sent[0]
    assert thread.mention in env.replies[-1]
    assert env.cog._thread_by_user["1"] == thread.id


def test_再按_帶回原本的討論串_不另開(env):
    old = FakeThread(500, archived=True, name="會議秘書小律｜Johnny")
    env.threads[500] = old
    env.cog._thread_by_user["1"] = 500

    press(env)

    assert env.parent.created == []
    assert old.archived is False, "封存的討論串要重新打開"
    assert old.name == "小律｜Johnny", "舊名稱順便改掉"
    assert old.sent == [], "帶回舊討論串時不重複打招呼"
    assert old.mention in env.replies[-1]


def test_原本的討論串不見了就開新的(env):
    env.cog._thread_by_user["1"] = 404

    press(env)

    assert len(env.parent.created) == 1


def test_不是藝人不開討論串(env):
    env.found["artist"] = None

    press(env)

    assert env.parent.created == [] and "找不到你的藝人資料" in env.replies[-1]


def test_按鈕重啟後仍可用():
    view = course_qa.StartXiaolu(cog=None)
    button = next(item for item in view.children if isinstance(item, discord.ui.Button))

    assert view.timeout is None and view.is_persistent()
    assert button.custom_id == course_qa.START_BUTTON_ID


def test_置頂說明講清楚用途_交件要換頻道(monkeypatch):
    monkeypatch.setattr(course_qa.config, "TASK_CHANNEL_ID", 123)
    text = course_qa.panel_text()

    assert "查任務的地方" in text and "不要直接在這個頻道打字" in text
    assert "不是在這裡" in text and "<#123>" in text and "/我的任務" in text
    assert "notion" not in text.lower()
    assert "<#123>" in course_qa.thread_welcome()


def test_沒設定任務頻道時寫頻道名稱(monkeypatch):
    monkeypatch.setattr(course_qa.config, "TASK_CHANNEL_ID", 0)

    assert "`任務查詢入口`" in course_qa.panel_text()


class FakeMessage:
    def __init__(self, message_id, content):
        self.id = message_id
        self.content = content
        self.pinned = False
        self.edited = False

    async def pin(self, **kwargs):
        self.pinned = True

    async def edit(self, *, content=None, view=None):
        self.content = content
        self.edited = True


class FakePanelChannel:
    def __init__(self, existing=None):
        self.id = 77
        self.sent: list[FakeMessage] = []
        self.existing = existing

    async def fetch_message(self, message_id):
        if self.existing and self.existing.id == message_id:
            return self.existing
        raise discord.NotFound(SimpleNamespace(status=404, reason="nf"), "nf")

    async def send(self, content, view=None):
        message = FakeMessage(1000 + len(self.sent), content)
        self.sent.append(message)
        return message


@pytest.fixture
def panel(monkeypatch, tmp_path):
    state = tmp_path / "panel.json"
    monkeypatch.setattr(course_qa, "_PANEL_STATE_PATH", state)
    monkeypatch.setattr(course_qa.discord, "TextChannel", FakePanelChannel)

    def make(channel):
        async def ready():
            return None

        cog = course_qa.CourseQACog.__new__(course_qa.CourseQACog)
        cog.bot = SimpleNamespace(wait_until_ready=ready, get_channel=lambda _id: channel)
        return cog

    return SimpleNamespace(state=state, make=make)


def test_沒有入口時發一則並置頂(panel):
    channel = FakePanelChannel()
    asyncio.run(panel.make(channel)._ensure_panel())

    assert len(channel.sent) == 1 and channel.sent[0].pinned
    assert '"message_id": 1000' in panel.state.read_text(encoding="utf-8")


def test_重啟時不重複發_內容有改才更新(panel):
    old = FakeMessage(55, "舊文案")
    channel = FakePanelChannel(existing=old)
    panel.state.write_text('{"channel_id": 77, "message_id": 55}', encoding="utf-8")

    asyncio.run(panel.make(channel)._ensure_panel())

    assert channel.sent == []
    assert old.edited and old.content == course_qa.panel_text() and old.pinned


def test_入口被刪掉就重發(panel):
    channel = FakePanelChannel()
    panel.state.write_text('{"channel_id": 77, "message_id": 55}', encoding="utf-8")

    asyncio.run(panel.make(channel)._ensure_panel())

    assert len(channel.sent) == 1
