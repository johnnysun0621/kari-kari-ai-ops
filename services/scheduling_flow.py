"""排課「確認 → 建正式課程 → 發通告」核心積木。

cog（/排課確認 的 FinalizeModal）與 web（老師外部確認連結）共用同一條流程：
建 Course Session → 排課需求轉已發布 → 公告區通告＋藝人 DM → 回寫通知紀錄 → Audit。

Notion 寫入失敗會往上拋（呼叫端決定怎麼回覆使用者）；
Discord 發送失敗只記 log 不中斷（通告發不出去不該讓已寫入的課程半途而廢）。
"""
import logging
from dataclasses import dataclass
from datetime import datetime

import discord

import config
from services import audit, repositories, timeparse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PublishResult:
    session_name: str
    session_url: str
    time_text: str
    notify_summary: str


async def publish_confirmed_schedule(
    client: discord.Client | None,
    request: repositories.ScheduleRequest,
    start: datetime,
    end: datetime,
    *,
    location: str = "",
    operator: str,
    discord_user_id: str = "",
) -> PublishResult:
    """老師（或控方）同意某時段後的完整落地流程。"""
    tenant = config.get_tenant()
    session_name = f"{request.course_type or '課程'}｜{start.strftime('%m/%d')}｜{request.name}"

    # 1) Notion 寫入（失敗上拋）
    session_page = await repositories.create_course_session(
        name=session_name,
        course_type=request.course_type or tenant.course_types[0],
        artist_page_ids=request.artist_page_ids,
        teacher=request.teacher,
        start=start,
        end=end,
        request_page_id=request.page_id,
        location=location,
    )
    await repositories.set_request_confirmed(
        request.page_id, start, end, session_page["id"],
        notice_url=session_page.get("url", ""),
    )

    # 2) 正式通告：公告區 + 藝人 DM（發送失敗不中斷）
    artists = await repositories.get_artists_by_ids(request.artist_page_ids)
    mentions = " ".join(f"<@{a.discord_user_id}>" for a in artists if a.discord_user_id)
    embed = discord.Embed(
        title=f"📢 正式通告｜{request.name}",
        description=(
            f"**課程**：{request.course_type}\n"
            f"**時間**：{timeparse.humanize(start, end)}\n"
            f"**老師**：{request.teacher or '待定'}\n"
            f"**地點**：{location or '待通知'}\n"
            f"**參與**：{'、'.join(a.name for a in artists)}"
        ),
        color=discord.Color.gold(),
    )
    notify_parts: list[str] = []
    channel = client.get_channel(tenant.resolved_announce_channel_id) if client else None
    if channel is not None:
        try:
            msg = await channel.send(content=mentions, embed=embed)
            notify_parts.append(f"頻道 {msg.jump_url}")
        except Exception:
            logger.exception("正式通告頻道發送失敗（channel=%s）", channel.id)
    dm_ok = 0
    if client is not None:
        for a in artists:
            if not a.discord_user_id:
                continue
            try:
                user = await client.fetch_user(int(a.discord_user_id))
                await user.send(embed=embed)
                dm_ok += 1
            except Exception:
                pass
    notify_parts.append(f"DM {dm_ok}/{len(artists)} 位")
    notify_record = (
        f"{datetime.now(tenant.tz).strftime('%Y-%m-%d %H:%M')}｜" + "、".join(notify_parts)
    )

    # 3) 回寫通知紀錄 + Audit（失敗不中斷）
    try:
        await repositories.mark_session_notified(session_page["id"], notify_record)
    except Exception:
        logger.exception("回寫通知紀錄失敗")
    await audit.log_event(
        event_name=f"發布正式通告｜{session_name}",
        action_type="建立",
        operator=operator,
        discord_user_id=discord_user_id,
        target_db="Course Sessions",
        target_url=session_page.get("url", ""),
        after=f"{timeparse.humanize(start, end)}｜{notify_record}",
    )
    return PublishResult(
        session_name=session_name,
        session_url=session_page.get("url", ""),
        time_text=timeparse.humanize(start, end),
        notify_summary="、".join(notify_parts),
    )
