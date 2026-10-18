"""每日 Drive 健康檢查：一天一次、失敗通知控方、恢復時說一聲、失敗時丟掉舊連線。"""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

import config
from cogs import channel_guard, reminders
from services import drive_api, drive_health, notification_log

TZ = config.TZ


class Clock:
    def __init__(self, now):
        self.now = now


@pytest.fixture
def env(monkeypatch):
    state: dict = {}
    sent: list[str] = []
    calls = {"check": 0, "drop": 0, "fail_with": None}
    clock = Clock(datetime(2026, 10, 19, 10, 5, tzinfo=TZ))

    monkeypatch.setattr(notification_log, "shared", lambda: state)
    monkeypatch.setattr(notification_log, "persist", lambda: None)
    monkeypatch.setattr(drive_api, "is_configured", lambda: True)

    async def check(folder_id):
        calls["check"] += 1
        if calls["fail_with"] is not None:
            raise calls["fail_with"]
        return {"id": folder_id}

    def drop():
        calls["drop"] += 1

    monkeypatch.setattr(drive_api, "check_folder_access", check)
    monkeypatch.setattr(drive_api, "drop_cached_service", drop)

    async def try_send(channel, content=None, **kwargs):
        sent.append(content)

    monkeypatch.setattr(channel_guard, "try_send", try_send)
    tenant = SimpleNamespace(tz=TZ, drive_root_folder_id="ROOT", daily_nag_hour=10)
    monkeypatch.setattr(config, "get_tenant", lambda: tenant)

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now

    monkeypatch.setattr(reminders, "datetime", FakeDatetime)
    cog = reminders.RemindersCog.__new__(reminders.RemindersCog)
    cog.bot = SimpleNamespace(get_channel=lambda _id: object())
    return SimpleNamespace(cog=cog, state=state, sent=sent, calls=calls, clock=clock, tenant=tenant)


def run(env):
    asyncio.run(env.cog._check_drive_health())


def test_正常時一天只讀一次_不通知(env):
    run(env)
    run(env)

    assert env.calls["check"] == 1
    assert env.sent == []
    assert env.state[drive_health.LAST_DATE_KEY] == "2026-10-19"


def test_還沒到時間不檢查(env):
    env.clock.now = datetime(2026, 10, 19, 9, 59, tzinfo=TZ)
    run(env)

    assert env.calls["check"] == 0


def test_授權失效當天通知一次並給修法(env):
    env.calls["fail_with"] = RuntimeError("('invalid_grant: Token has been expired or revoked.')")
    run(env)
    run(env)  # 同一天第二輪不再通知

    assert len(env.sent) == 1
    assert "授權失效" in env.sent[0] and "authorize_drive.py" in env.sent[0]
    assert env.state[drive_health.FAILING_KEY] is True


def test_隔天恢復會說一聲_之後不再說(env):
    env.calls["fail_with"] = RuntimeError("invalid_grant")
    run(env)
    env.calls["fail_with"] = None
    env.clock.now = datetime(2026, 10, 20, 10, 0, tzinfo=TZ)
    run(env)
    env.clock.now = datetime(2026, 10, 21, 10, 0, tzinfo=TZ)
    run(env)

    assert env.sent[1] == drive_health.RECOVERED_MESSAGE
    assert len(env.sent) == 2
    assert drive_health.FAILING_KEY not in env.state


def test_每次檢查都重建連線_重新授權後不必重啟(env):
    run(env)

    assert env.calls["drop"] >= 1


def test_沒設定總資料夾就不檢查(env):
    env.tenant.drive_root_folder_id = ""
    run(env)

    assert env.calls["check"] == 0 and env.sent == []


@pytest.mark.parametrize("message,reason", [
    ("invalid_grant: Bad Request", "授權失效"),
    ("<HttpError 404 ... File not found: ROOT>", "找不到總資料夾"),
    ("<HttpError 403 insufficientPermissions>", "沒有總資料夾的權限"),
    ("timed out", "Drive 連線失敗"),
])
def test_錯誤分類(message, reason):
    assert drive_health.classify_error(RuntimeError(message)) == reason


def test_通知不帶_Notion_網址():
    for reason in drive_health.FIX_STEPS:
        assert "notion" not in drive_health.failure_message(reason, "x").lower()
