"""Discord slash command channel routing helpers."""
import discord

import config


def _is_allowed_channel(expected_channel_id: int, actual_channel_id: int | None) -> bool:
    return not expected_channel_id or actual_channel_id == expected_channel_id


def _channel_message(expected_channel_id: int, command_kind: str) -> str:
    if not expected_channel_id:
        return ""
    return f"請到 <#{expected_channel_id}> 使用這個{command_kind}指令。"


def is_schedule_channel(channel_id: int | None) -> bool:
    """時間池 / 排課指令：有設定 SCHEDULE_CHANNEL_ID 時，只允許在時間資訊彙整使用。"""
    return _is_allowed_channel(config.SCHEDULE_CHANNEL_ID, channel_id)


def is_task_channel(channel_id: int | None) -> bool:
    """任務指令：有設定 TASK_CHANNEL_ID 時，只允許在任務查詢入口使用。"""
    return _is_allowed_channel(config.TASK_CHANNEL_ID, channel_id)


def schedule_channel_message() -> str:
    return _channel_message(config.SCHEDULE_CHANNEL_ID, "時間 / 排課")


def task_channel_message() -> str:
    return _channel_message(config.TASK_CHANNEL_ID, "任務")


async def ensure_schedule_channel(interaction: discord.Interaction) -> bool:
    if is_schedule_channel(interaction.channel_id):
        return True
    await interaction.response.send_message(schedule_channel_message(), ephemeral=True)
    return False


async def ensure_task_channel(interaction: discord.Interaction) -> bool:
    if is_task_channel(interaction.channel_id):
        return True
    await interaction.response.send_message(task_channel_message(), ephemeral=True)
    return False
