"""Discord slash command channel routing helpers."""
import logging

import discord

import config

logger = logging.getLogger(__name__)


async def try_send(channel, **kwargs) -> discord.Message | None:
    """發訊息到頻道，權限不足或頻道不存在時記 log 不拋例外。

    附帶通知（控方通知、升級訊息）用這個：通知失敗不該毀掉主流程
    （例如藝人提交成功後的確認回覆）。
    """
    if channel is None:
        return None
    try:
        return await channel.send(**kwargs)
    except discord.Forbidden:
        logger.warning(
            "無權發送訊息到頻道 %s（請確認 bot 有檢視＋發送權限）",
            getattr(channel, "id", "?"),
        )
        return None
    except discord.HTTPException:
        logger.exception("發送頻道訊息失敗（channel=%s）", getattr(channel, "id", "?"))
        return None


def _is_allowed_channel(expected_channel_id: int, actual_channel_id: int | None) -> bool:
    return not expected_channel_id or actual_channel_id == expected_channel_id


def _channel_message(expected_channel_id: int, command_kind: str) -> str:
    if not expected_channel_id:
        return ""
    return f"請到 <#{expected_channel_id}> 使用這個{command_kind}指令。"


def announce_channel_id() -> int:
    """公告發送目標：正式通告與收集/排課公告。

    有設定 ANNOUNCE_CHANNEL_ID 就發公告區（唯讀頻道也能點按鈕）；
    未設定時 fallback 回時間資訊彙整（SCHEDULE_CHANNEL_ID），行為與舊版相同。
    """
    return config.ANNOUNCE_CHANNEL_ID or config.SCHEDULE_CHANNEL_ID


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
