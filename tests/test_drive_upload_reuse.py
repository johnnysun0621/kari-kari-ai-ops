"""重新進入同一筆任務的交件要回到同一個 ver 資料夾。

2026-10-18 QA：上傳入口是 ephemeral 訊息，藝人離開去 Drive 上傳、回來訊息就不見，
重新進入後 bot 每次都開新的 ver（實測一筆任務開到 ver.4，ver.1～3 是空的），
先前上傳的檔案留在舊夾，「我已上傳到 Drive」去新夾驗證會找不到。
"""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import config
from cogs import submissions
from services import drive_api, repositories

NOW = datetime(2026, 10, 18, 11, 0, tzinfo=config.TZ)


def make_task(page_id="t1"):
    return repositories.TaskItem(
        page_id=page_id, name="提供稿件", status="待提交", task_type=None,
        artist_page_ids=["a1"], project_page_ids=["p1"],
        end=datetime(2026, 10, 31, tzinfo=config.TZ),
    )


ARTIST = repositories.Artist(page_id="a1", name="Johnny", discord_user_id="1",
                             drive_folder_url="https://drive.google.com/drive/folders/Zone")


class FakeInteraction:
    def __init__(self, user_id=1):
        self.user = SimpleNamespace(id=user_id)
        self.channel_id = 99
        self.sent: list[str] = []

        async def defer(**kwargs):
            return None

        async def send(content="", **kwargs):
            self.sent.append(content)

        self.response = SimpleNamespace(defer=defer)
        self.followup = SimpleNamespace(send=send)


@pytest.fixture
def env(monkeypatch, tmp_path):
    state_path = tmp_path / "pending_drive_uploads.json"
    monkeypatch.setattr(submissions, "DRIVE_UPLOAD_STATE_PATH", state_path)
    created: list[str] = []

    async def find_artist(_uid):
        return ARTIST

    async def resolve(task, artist, now):
        return ("Zone", ["專案", "任務"]), ""

    async def next_version(*, base_folder_id, parent_path, item_name, created_at):
        n = len(created) + 1
        created.append(f"ver.{n}")
        return {"webViewLink": f"https://drive.google.com/drive/folders/V{n}"}, [*parent_path, f"ver.{n}"]

    monkeypatch.setattr(repositories, "find_artist_by_discord_id", find_artist)
    monkeypatch.setattr(submissions, "_resolve_submission_target", resolve)
    monkeypatch.setattr(submissions, "_ensure_next_version_folder", next_version)
    monkeypatch.setattr(drive_api, "is_configured", lambda: True)
    clock = {"now": NOW}

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    monkeypatch.setattr(submissions, "datetime", FakeDatetime)
    cog = submissions.SubmissionsCog.__new__(submissions.SubmissionsCog)
    cog.pending_file_submissions = {}
    return SimpleNamespace(cog=cog, created=created, clock=clock, state_path=state_path)


def enter(env, task, user_id=1):
    interaction = FakeInteraction(user_id)
    asyncio.run(env.cog.start_file_upload_wait(interaction, task))
    return interaction


def test_重新進入同一任務沿用同一個資料夾(env):
    enter(env, make_task())
    env.clock["now"] = NOW + timedelta(minutes=20)
    again = enter(env, make_task())

    assert env.created == ["ver.1"]
    pending = env.cog.pending_file_submissions[1]
    assert pending.folder_url.endswith("/V1")
    assert pending.created_at == NOW, "要沿用第一次的時間，才驗證得到先前上傳的檔案"
    assert "沿用你上次開的資料夾" in again.sent[0]


def test_bot重啟後還找得回資料夾(env):
    enter(env, make_task())
    env.cog.pending_file_submissions.clear()  # 重啟：記憶體清空，只剩 .state

    enter(env, make_task())

    assert env.created == ["ver.1"]
    assert env.cog.pending_file_submissions[1].folder_url.endswith("/V1")


def test_入口過期後才開新版本(env):
    enter(env, make_task())
    env.clock["now"] = NOW + timedelta(minutes=submissions._file_upload_wait_minutes() + 1)

    enter(env, make_task())

    assert env.created == ["ver.1", "ver.2"]


def test_不同任務各自一個資料夾_切回來仍沿用(env):
    enter(env, make_task("A"))
    enter(env, make_task("B"))
    enter(env, make_task("A"))

    assert env.created == ["ver.1", "ver.2"]
    assert env.cog.pending_file_submissions[1].folder_url.endswith("/V1")


def test_別人進同一任務不會拿到你的資料夾(env):
    enter(env, make_task(), user_id=1)
    enter(env, make_task(), user_id=2)

    assert env.created == ["ver.1", "ver.2"]


def test_提交成功後移除_下次再交才開新版本():
    state = {"1": {"t1": {"folder_url": "u"}, "t2": {"folder_url": "v"}}}
    assert submissions.forget_drive_upload(state, 1, "t1") == {"1": {"t2": {"folder_url": "v"}}}
    assert submissions.forget_drive_upload(state, 1, "t2") == {}


def test_壞掉的紀錄不會讓交件流程出錯():
    assert submissions.reusable_drive_upload({"1": {"t1": {"folder_url": "u"}}}, 1, "t1", NOW) is None
    assert submissions.reusable_drive_upload({"1": "亂掉"}, 1, "t1", NOW) is None
