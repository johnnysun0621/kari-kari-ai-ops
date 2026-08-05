"""Task 正式安排的 Discord 通知流程。"""
import logging
from dataclasses import dataclass
from datetime import datetime

import discord

import config
from services import audit, formal_notice, repositories, scheduling_sla, timeparse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaskNoticeResult:
    task_name: str
    task_url: str
    time_text: str
    notify_summary: str


async def publish_task_notice(
    client: discord.Client | None,
    *,
    task: repositories.TaskItem,
    artists: list[repositories.Artist],
    teachers: list[repositories.PersonItem],
    operator: str,
    discord_user_id: str = "",
) -> TaskNoticeResult:
    """預覽確認後發布既有 Task；任何一個管道成功即視為已發布。"""
    event_name = f"發布正式通告｜{task.name}"
    if formal_notice.was_published(task.page_id) or await audit.has_successful_event(
        event_name, task.url
    ):
        formal_notice.mark_published(task.page_id)
        raise ValueError("task_already_published")
    if not repositories.is_publishable_formal_task(task):
        raise ValueError("task_not_publishable")

    time_text = timeparse.humanize(task.start, task.end)
    arrangement_type = task.course_category or task.task_type or "正式安排"
    location = repositories.task_description_field(
        task.description, "地點", "地點／連結"
    )
    mentions = " ".join(f"<@{a.discord_user_id}>" for a in artists if a.discord_user_id)
    embed = discord.Embed(
        title=f"📢 正式通告｜{task.name}",
        description=(
            f"**安排類型**：{arrangement_type}\n"
            f"**時間**：{time_text}\n"
            f"**地點／連結**：{location or '未填'}\n"
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
    teacher_ok = 0
    teacher_missing: list[str] = []
    if client is not None:
        for teacher in teachers:
            if not teacher.discord_user_id:
                teacher_missing.append(teacher.name)
                continue
            try:
                user = await client.fetch_user(int(teacher.discord_user_id))
                await user.send(embed=embed)
                teacher_ok += 1
            except Exception:
                teacher_missing.append(teacher.name)
                logger.exception("正式安排老師 DM 失敗（teacher=%s）", teacher.name)
    if teachers:
        notify_parts.append(f"老師 DM {teacher_ok}/{len(teachers)} 位")
    notify_record = (
        f"{datetime.now(tenant.tz).strftime('%Y-%m-%d %H:%M')}｜" + "、".join(notify_parts)
    )
    delivery_count = (1 if any(part.startswith("頻道 ") for part in notify_parts) else 0) + dm_ok + teacher_ok
    if delivery_count == 0:
        raise RuntimeError("formal_notice_delivery_failed")
    await repositories.mark_task_formally_published(task.page_id)
    formal_notice.mark_published(task.page_id)
    scheduling_sla.mark_completed(task.page_id, int(datetime.now(tenant.tz).timestamp()))
    await audit.log_event(
        event_name=event_name,
        action_type="建立",
        operator=operator,
        discord_user_id=discord_user_id,
        target_db="Tasks",
        target_url=task.url,
        after=f"{arrangement_type}｜{time_text}｜{notify_record}",
    )
    return TaskNoticeResult(
        task_name=task.name,
        task_url=task.url,
        time_text=time_text,
        notify_summary=(
            "、".join(notify_parts)
            + (f"｜需人工轉傳老師：{'、'.join(teacher_missing)}" if teacher_missing else "")
        ),
    )
