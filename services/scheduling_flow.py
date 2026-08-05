"""Task 正式安排的 Discord 通知流程。"""
import logging
from dataclasses import dataclass
from datetime import datetime

import discord

import config
from services import audit, repositories, timeparse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DirectPublishResult:
    task_name: str
    task_url: str
    time_text: str
    notify_summary: str


async def publish_direct_arrangement(
    client: discord.Client | None,
    *,
    arrangement_type: str,
    task: repositories.TaskItem,
    artists: list[repositories.Artist],
    start: datetime,
    end: datetime,
    location: str = "",
    operator: str,
    discord_user_id: str = "",
) -> DirectPublishResult:
    """發布錄音／通告等不需要老師外部確認的正式安排。"""
    time_text = timeparse.humanize(start, end)
    mentions = " ".join(f"<@{a.discord_user_id}>" for a in artists if a.discord_user_id)
    embed = discord.Embed(
        title=f"📢 正式通告｜{task.name}",
        description=(
            f"**安排類型**：{arrangement_type}\n"
            f"**時間**：{time_text}\n"
            f"**地點／連結**：{location or '待通知'}\n"
            f"**參與**：{'、'.join(a.name for a in artists)}"
        ),
        color=discord.Color.gold(),
    )
    notify_parts: list[str] = []
    tenant = config.get_tenant()
    channel = client.get_channel(tenant.resolved_announce_channel_id) if client else None
    if channel is not None:
        try:
            msg = await channel.send(content=mentions, embed=embed)
            notify_parts.append(f"頻道 {msg.jump_url}")
        except Exception:
            logger.exception("正式通告頻道發送失敗（channel=%s）", channel.id)
    elif client is not None:
        notify_parts.append("⚠️ 找不到公告頻道")

    dm_ok = 0
    if client is not None:
        for artist in artists:
            if not artist.discord_user_id:
                continue
            try:
                user = await client.fetch_user(int(artist.discord_user_id))
                await user.send(embed=embed)
                dm_ok += 1
            except Exception:
                logger.exception("正式安排 DM 失敗（artist=%s）", artist.name)
    notify_parts.append(f"DM {dm_ok}/{len(artists)} 位")
    notify_record = (
        f"{datetime.now(tenant.tz).strftime('%Y-%m-%d %H:%M')}｜" + "、".join(notify_parts)
    )
    await audit.log_event(
        event_name=f"發布正式通告｜{task.name}",
        action_type="建立",
        operator=operator,
        discord_user_id=discord_user_id,
        target_db="Tasks",
        target_url=task.url,
        after=f"{arrangement_type}｜{time_text}｜{notify_record}",
    )
    return DirectPublishResult(
        task_name=task.name,
        task_url=task.url,
        time_text=time_text,
        notify_summary="、".join(notify_parts),
    )
