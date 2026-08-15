"""Slash command visibility metadata tests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs import channel_guard
from discord import app_commands
from cogs.bulletin import BulletinCog
from cogs.scheduling_artist import SchedulingArtistCog
from cogs.scheduling_admin import SchedulingAdminCog
from cogs.tasks_artist import TasksCog
from cogs.operations_qa import OperationsQACog
from cogs.server_setup import ServerSetupCog
from cogs.submissions import SubmissionsCog
from cogs.course_qa import CourseQACog


ADMIN_COMMANDS = [
    SchedulingAdminCog.create_availability_collection,
    SchedulingAdminCog.match_availability_pool,
    TasksCog.task_overview,
    OperationsQACog.operations_query,
    ServerSetupCog.initialize_server,
]


def test_admin_commands_are_hidden_from_non_admins():
    for command in ADMIN_COMMANDS:
        assert command.default_permissions is not None, command.name
        assert command.default_permissions.administrator is True, command.name


def test_legacy_scheduling_commands_removed_from_tree():
    """舊排課架構已刪除，四個舊指令不得出現在指令樹。"""
    import asyncio

    import discord
    from discord.ext import commands as dpy_commands

    async def load():
        bot = dpy_commands.Bot(command_prefix="!", intents=discord.Intents.default())
        await bot.load_extension("cogs.scheduling_admin")
        return {c.name for c in bot.tree.get_commands()}

    names = asyncio.run(load())
    assert not ({"排課建立", "排課比對", "排課確認", "排課狀態"} & names)
    assert "從時間池建立任務" in names
    assert "時間池整理" not in names
    assert "正式通告發布" not in names


def test_time_collection_command_uses_task_focused_name():
    assert SchedulingAdminCog.create_availability_collection.name == "時間資訊蒐集"


def test_duplicate_artist_commands_are_removed():
    assert not hasattr(SubmissionsCog, "submit_task")
    assert not hasattr(CourseQACog, "course_question")


def test_registered_cogs_expose_exactly_ten_slash_commands():
    cog_classes = (
        SchedulingAdminCog,
        SchedulingArtistCog,
        TasksCog,
        SubmissionsCog,
        BulletinCog,
        CourseQACog,
        OperationsQACog,
        ServerSetupCog,
    )
    names = {
        value.name
        for cog_class in cog_classes
        for value in vars(cog_class).values()
        if isinstance(value, app_commands.Command)
    }
    assert names == {
        "伺服器初始化",
        "布告欄同步",
        "我的任務",
        "任務總覽",
        "任務通知重送",
        "營運查詢",
        "時間資訊蒐集",
        "從時間池建立任務",
        "管理可配合時間",
        "審核提交",
    }


def test_management_task_commands_use_admin_channel_guard(monkeypatch):
    guard = AsyncMock(return_value=False)
    monkeypatch.setattr(channel_guard, "ensure_admin_channel", guard)
    interaction = SimpleNamespace()

    async def run():
        cases = (
            (SchedulingAdminCog.match_availability_pool, ("2026-08", 60)),
            (TasksCog.task_overview, ()),
            (TasksCog.resend_task_notification, ()),
            (SubmissionsCog.review_submission, ()),
            (BulletinCog.sync_command, ()),
        )
        for command, args in cases:
            guard.reset_mock()
            await command.callback(SimpleNamespace(), interaction, *args)
            guard.assert_awaited_once_with(interaction)

    asyncio.run(run())

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


def test_admin_channel_restricts_management_ai_when_configured(monkeypatch):
    monkeypatch.setattr(channel_guard.config, "ADMIN_COMMAND_CHANNEL_ID", 987)

    assert channel_guard.is_admin_channel(987)
    assert not channel_guard.is_admin_channel(123)
    assert channel_guard.admin_channel_message() == "請到 <#987> 使用這個營運管理指令。"


def test_announce_channel_falls_back_to_schedule_channel(monkeypatch):
    monkeypatch.setattr(channel_guard.config, "ANNOUNCE_CHANNEL_ID", 0)
    monkeypatch.setattr(channel_guard.config, "SCHEDULE_CHANNEL_ID", 789)

    assert channel_guard.announce_channel_id() == 789  # 未設定＝行為不變


def test_announce_channel_used_when_configured(monkeypatch):
    monkeypatch.setattr(channel_guard.config, "ANNOUNCE_CHANNEL_ID", 555)
    monkeypatch.setattr(channel_guard.config, "SCHEDULE_CHANNEL_ID", 789)

    assert channel_guard.announce_channel_id() == 555
