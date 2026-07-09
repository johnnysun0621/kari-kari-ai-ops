"""Slash command visibility metadata tests."""

from cogs import channel_guard
from cogs.scheduling_admin import SchedulingAdminCog
from cogs.tasks_artist import TasksCog


ADMIN_COMMANDS = [
    SchedulingAdminCog.create_availability_collection,
    SchedulingAdminCog.cleanup_availability_pool,
    SchedulingAdminCog.match_availability_pool,
    SchedulingAdminCog.create_schedule,
    SchedulingAdminCog.match_schedule,
    SchedulingAdminCog.confirm_schedule,
    SchedulingAdminCog.schedule_status,
    TasksCog.task_overview,
]


def test_admin_commands_are_hidden_from_non_admins():
    for command in ADMIN_COMMANDS:
        assert command.default_permissions is not None, command.name
        assert command.default_permissions.administrator is True, command.name


def test_task_channel_allows_anywhere_when_not_configured(monkeypatch):
    monkeypatch.setattr(channel_guard.config, "TASK_CHANNEL_ID", 0)

    assert channel_guard.is_task_channel(123)
    assert channel_guard.is_task_channel(None)


def test_task_channel_restricts_when_configured(monkeypatch):
    monkeypatch.setattr(channel_guard.config, "TASK_CHANNEL_ID", 456)

    assert channel_guard.is_task_channel(456)
    assert not channel_guard.is_task_channel(123)
    assert channel_guard.task_channel_message() == "請到 <#456> 使用這個任務指令。"


def test_schedule_channel_allows_anywhere_when_not_configured(monkeypatch):
    monkeypatch.setattr(channel_guard.config, "SCHEDULE_CHANNEL_ID", 0)

    assert channel_guard.is_schedule_channel(123)
    assert channel_guard.is_schedule_channel(None)


def test_schedule_channel_restricts_when_configured(monkeypatch):
    monkeypatch.setattr(channel_guard.config, "SCHEDULE_CHANNEL_ID", 789)

    assert channel_guard.is_schedule_channel(789)
    assert not channel_guard.is_schedule_channel(123)
    assert channel_guard.schedule_channel_message() == "請到 <#789> 使用這個時間 / 排課指令。"
