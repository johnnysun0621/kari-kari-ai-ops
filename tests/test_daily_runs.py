"""M3-11：每天一次的發送（藝人早報、營運早報、每日催辦）全部送達才算完成，失敗同日重試。"""
import asyncio
import dataclasses
from datetime import datetime
from types import SimpleNamespace

import pytest

import config
from cogs import briefing, ops_briefing as ops_cog, reminders as reminders_cog
from services import audit, daily_runs, notification_log

TZ = config.TZ
NOW = datetime(2026, 10, 22, 9, 40, tzinfo=TZ)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(notification_log, "STATE_PATH", tmp_path / "log.json")
    notification_log.reset_for_tests()
    monkeypatch.setattr(config, "TENANT", dataclasses.replace(
        config.TENANT, daily_briefing_enabled=True, daily_briefing_hour=9,
        ops_briefing_enabled=True, ops_briefing_hour=9, ops_briefing_minute=30))
    monkeypatch.setattr(reminders_cog, "datetime", SimpleNamespace(now=lambda tz=None: NOW))

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(audit, "log_event", noop)
    yield
    notification_log.reset_for_tests()


def cog():
    return SimpleNamespace(bot=object(), _sent={})


def run(method):
    asyncio.run(getattr(reminders_cog.RemindersCog, method)(cog()))


# ---------- 狀態本身 ----------

def test_全部送達才算完成_最多試三次():
    state = {}
    for attempt in range(1, daily_runs.MAX_ATTEMPTS + 1):
        assert daily_runs.should_run(state, "x", NOW)
        assert daily_runs.begin(state, "x", NOW) == attempt
        done = daily_runs.finish(state, "x", NOW, complete=False)
        assert done == (attempt == daily_runs.MAX_ATTEMPTS)
    assert not daily_runs.should_run(state, "x", NOW)


def test_成功一次就不再跑_隔天重新開始():
    state = {}
    daily_runs.begin(state, "x", NOW)
    daily_runs.finish(state, "x", NOW, complete=True)
    assert not daily_runs.should_run(state, "x", NOW)
    assert daily_runs.should_run(state, "x", NOW.replace(day=23))


def test_上線當天已用舊版跑過的_不再跑一次():
    assert not daily_runs.should_run({}, "x", NOW, legacy_last_date="2026-10-22")
    assert daily_runs.should_run({}, "x", NOW, legacy_last_date="2026-10-21")


# ---------- 藝人早報 ----------

def test_早報有人失敗_下一輪重試_最後一次才清佇列(monkeypatch):
    results = [{"failed": 1}, {"failed": 0}]
    calls = []

    async def fake_send(client, now, *, final=True):
        calls.append(final)
        r = results[len(calls) - 1]
        return {"sent": 1, "cleared": 0, "tasks": 1, "staff_sent": 0, **r}

    monkeypatch.setattr(briefing, "send_daily_briefings", fake_send)
    run("_check_daily_briefing")
    assert notification_log.shared().get("briefing_last_date") is None
    run("_check_daily_briefing")
    run("_check_daily_briefing")  # 第二次已全部送達：不再跑
    assert calls == [False, False]
    assert notification_log.shared()["briefing_last_date"] == "2026-10-22"


def test_早報讀資料失敗_不算完成(monkeypatch):
    calls = []

    async def boom(client, now, *, final=True):
        calls.append(final)
        if len(calls) == 1:
            raise RuntimeError("Notion 503")
        return {"sent": 1, "cleared": 0, "tasks": 1, "staff_sent": 0, "failed": 0}

    monkeypatch.setattr(briefing, "send_daily_briefings", boom)
    run("_check_daily_briefing")
    run("_check_daily_briefing")
    assert len(calls) == 2


def test_早報一直失敗_最多三次_第三次是最後一次(monkeypatch):
    calls = []

    async def always_fail(client, now, *, final=True):
        calls.append(final)
        return {"sent": 0, "cleared": 0, "tasks": 0, "staff_sent": 0, "failed": 2}

    monkeypatch.setattr(briefing, "send_daily_briefings", always_fail)
    for _ in range(5):
        run("_check_daily_briefing")
    assert calls == [False, False, True]


def test_早報_重啟後記得已完成(monkeypatch):
    calls = []

    async def ok(client, now, *, final=True):
        calls.append(final)
        return {"sent": 1, "cleared": 0, "tasks": 1, "staff_sent": 0, "failed": 0}

    monkeypatch.setattr(briefing, "send_daily_briefings", ok)
    run("_check_daily_briefing")
    notification_log.reset_for_tests()
    run("_check_daily_briefing")
    assert len(calls) == 1


# ---------- 營運早報 ----------

def test_營運早報沒發出去_下一輪重試(monkeypatch):
    results = [False, True]
    calls = []

    async def fake_send(client, now):
        calls.append(now)
        return results[len(calls) - 1]

    monkeypatch.setattr(ops_cog, "send_ops_briefing", fake_send)
    run("_check_ops_briefing")
    run("_check_ops_briefing")
    run("_check_ops_briefing")
    assert len(calls) == 2
    assert notification_log.shared()["ops_briefing_last_date"] == "2026-10-22"


def test_營運早報時間還沒到不跑(monkeypatch):
    calls = []

    async def fake_send(client, now):
        calls.append(now)
        return True

    monkeypatch.setattr(ops_cog, "send_ops_briefing", fake_send)
    monkeypatch.setattr(reminders_cog, "datetime", SimpleNamespace(now=lambda tz=None: NOW.replace(hour=9, minute=10)))
    run("_check_ops_briefing")
    assert calls == []
