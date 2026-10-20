"""控方「要處理」通知的「🙋 我來處理／✅ 已處理」按鈕（M3-25，2026-10-20）。

原本頻道裡的通知發出去就沒人知道處理了沒，越堆越長。現在：
- 我來處理：訊息標上「👤 某某 處理中（時間）」，其他人知道有人接了
- 已處理：整則收成一行「✅ 已處理｜標題｜誰 時間」，按鈕拿掉
狀態就存在訊息本身（不寫 Notion、不靠 .state），重啟後舊訊息的按鈕仍可按；每次按都寫稽核紀錄。
"""
import logging
import re
from datetime import datetime

import discord

import config
from services import audit, ops_notice

logger = logging.getLogger(__name__)

_OWNER_LINE_RE = re.compile(r"\n👤 .+ 處理中（\d{2}/\d{2} \d{2}:\d{2}）$")


def _actor(interaction: discord.Interaction) -> str:
    user = interaction.user
    return getattr(user, "display_name", None) or getattr(user, "name", "") or str(user.id)


def claimed_content(content: str, actor: str, now: datetime) -> str:
    """加上（或換掉）「處理中」那一行；同一則只會有一行。"""
    base = _OWNER_LINE_RE.sub("", content or "")
    return f"{base}\n👤 {actor} 處理中（{now:%m/%d %H:%M}）"[:2000]


def done_content(content: str, actor: str, now: datetime) -> str:
    return f"✅ 已處理｜{ops_notice.first_line(content)}｜{actor} {now:%m/%d %H:%M}"[:2000]


class OpsClaimButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ops_claim"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="我來處理", emoji="🙋", style=discord.ButtonStyle.primary,
                                           custom_id="ops_claim"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        now = datetime.now(config.get_tenant().tz)
        actor = _actor(interaction)
        content = claimed_content(interaction.message.content if interaction.message else "", actor, now)
        await interaction.response.edit_message(content=content)
        await audit.log_event(
            event_name=f"控方認領｜{ops_notice.first_line(content)}"[:200],
            action_type="更新", operator=actor, discord_user_id=str(interaction.user.id),
            target_db="Discord", after="處理中",
        )


class OpsDoneButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ops_done"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="已處理", emoji="✅", style=discord.ButtonStyle.success,
                                           custom_id="ops_done"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        now = datetime.now(config.get_tenant().tz)
        actor = _actor(interaction)
        original = interaction.message.content if interaction.message else ""
        await interaction.response.edit_message(content=done_content(original, actor, now), view=None)
        await audit.log_event(
            event_name=f"控方已處理｜{ops_notice.first_line(original)}"[:200],
            action_type="更新", operator=actor, discord_user_id=str(interaction.user.id),
            target_db="Discord", after="已處理",
        )


def ops_action_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(OpsClaimButton())
    view.add_item(OpsDoneButton())
    return view
