"""控方每日營運早報：收集資料、發送與「看某一筆的詳細」（M3-25 第二階段）。規則與文案在 services/ops_briefing.py。

不是 cog：由 cogs/reminders.py 呼叫 send_ops_briefing()，選單在 bot.py 註冊（DynamicItem，重啟後仍可用）。
"""
import logging
from datetime import date, datetime, timedelta

import discord

import config
from cogs import channel_guard, ui_text
from services import daily_nag, notification_log, ops_briefing, repositories, task_notifications, task_query, time_collection

logger = logging.getLogger(__name__)


def _day_code(day: date) -> str:
    return day.strftime("%Y%m%d")


def _names(task: repositories.TaskItem, artist_map: dict) -> list[str]:
    return [artist_map[pid].name for pid in task.artist_page_ids if pid in artist_map]


async def collect(now: datetime, state: dict) -> ops_briefing.OpsBriefing:
    tenant = config.get_tenant()
    tasks = await repositories.get_active_tasks()
    artist_map = await repositories.get_artist_map()
    tasks_by_id = {task.page_id: task for task in tasks}
    b = ops_briefing.OpsBriefing(review_after_hours=tenant.review_reminder_after_hours)

    new_ids = ops_briefing.new_overdue_ids(state)
    for task in tasks:
        if task.status != "已逾期":
            continue
        if task.page_id in new_ids:
            b.new_overdue.append(ops_briefing.task_line(
                task.page_id, _names(task, artist_map), task.name, ui_text.deadline_text(task, now), "新逾期", task.url))
        else:
            for name in _names(task, artist_map) or ["（未關聯藝人）"]:
                b.still_overdue_counts[name] = b.still_overdue_counts.get(name, 0) + 1

    for task in tasks:
        if repositories.task_awaits_ops_confirmation(task, now):
            b.awaiting_confirmation.append(ops_briefing.task_line(
                task.page_id, _names(task, artist_map), task.name, ui_text.deadline_text(task, now),
                "已過截止，等你確認結案", task.url))

    for task in tasks:
        if task.status == "需修改" and task.revision_deadline and task.revision_deadline < now:
            b.revision_passed.append(ops_briefing.task_line(
                task.page_id, _names(task, artist_map), task.name,
                f"修改期限 {task.revision_deadline:%m/%d}", "修改期限已過", task.url))

    try:
        from cogs.reminders import review_reminder_due

        submissions = await repositories.get_submissions_by_review_status(["待審核"])
        pending = daily_nag.pending_review_tasks(
            submissions, tasks_by_id, now, tenant.review_reminder_after_hours, review_reminder_due)
        reviewer_ids = sorted({pid for tid in pending for pid in tasks_by_id[tid].reviewer_page_ids})
        reviewers = {p.page_id: p for p in await repositories.get_people_by_ids(reviewer_ids)}
        unreachable = ops_briefing.unreachable_review_ids(state)
        for task_id, subs in pending.items():
            task = tasks_by_id[task_id]
            if task_id not in unreachable and any(pid in reviewers and reviewers[pid].discord_user_id for pid in task.reviewer_page_ids):
                continue  # 有人負責：10:00 的每日催辦會私訊審核人員
            oldest = min((s.created_at for s in subs if s.created_at), default=None)
            hours = int((now - oldest).total_seconds() // 3600) if oldest else None
            b.review_unassigned.append(ops_briefing.task_line(
                task_id, _names(task, artist_map), task.name,
                f"已 {hours} 小時" if hours is not None else "", "待審核沒人負責", task.url))
    except Exception:
        logger.exception("營運早報：讀取待審核失敗，這一區先略過")

    cutoff = now - timedelta(days=ops_briefing.UNRECEIVED_DAYS)
    for task in tasks:
        # 只看有關聯藝人的：只派給相關同仁的任務沒有「收到任務」按鈕，不會有人按
        if (task.artist_page_ids and task.discord_visible and task.discord_acknowledged_at is None
                and task_notifications.can_acknowledge(task)
                and task.discord_notified_at and task.discord_notified_at <= cutoff):
            b.unreceived.append(ops_briefing.task_line(
                task.page_id, _names(task, artist_map), task.name,
                f"{task.discord_notified_at:%m/%d} 派發", "還沒按收到", task.url))

    b.unread_artists = ops_briefing.unread_for(state, _day_code(now.date() - timedelta(days=1)))
    b.teacher_notes = ops_briefing.teacher_notes(state)

    for collection in time_collection.active_collections():
        try:
            ids = list(collection.artist_page_ids)
            artists = {a.page_id: a for a in await repositories.get_artists_by_ids(ids)}
            progress = await repositories.get_monthly_pool_progress(collection.month, ids)
            missing = [artists[pid].name for pid in progress.missing_artist_page_ids if pid in artists]
            deadline = datetime.fromtimestamp(collection.deadline_ts, tz=tenant.tz)
            b.pools.append(
                f"{collection.month}：已填 {len(ids) - len(progress.missing_artist_page_ids)}／{len(ids)}"
                + (f"｜還沒填 {'、'.join(missing)}" if missing else "")
                + f"｜截止 {deadline:%m/%d %H:%M}"
            )
        except Exception:
            logger.exception("營運早報：時間池進度讀取失敗：%s", collection.month)

    weekday = tenant.publish_confirm_digest_weekday
    if weekday < 0 or now.weekday() == weekday:
        try:
            waiting = repositories.tasks_awaiting_publish_confirmation(
                await repositories.get_tasks_by_status(["已完成"]), now, tenant.publish_confirm_digest_after_days)
            for task in waiting:
                b.publish_waiting.append(ops_briefing.task_line(
                    task.page_id, _names(task, artist_map), task.name, ui_text.deadline_text(task, now), "發文待確認", task.url))
        except Exception:
            logger.exception("營運早報：發文待確認讀取失敗")
    return b


class OpsDetailSelect(
    discord.ui.DynamicItem[discord.ui.Select],
    template=r"ops_detail:(?P<day>\d{8})",
):
    """營運早報的「看某一筆的詳細」：展開在同一則（embed），控方版附 Notion 連結。"""

    def __init__(self, day: str, options: list[discord.SelectOption] | None = None):
        self.options = options or [discord.SelectOption(label="（沒有任務）", value="none")]
        super().__init__(discord.ui.Select(
            placeholder="看某一筆的詳細…", options=self.options,
            custom_id=f"ops_detail:{day}", min_values=1, max_values=1,
        ))
        self.day = day

    @classmethod
    async def from_custom_id(cls, interaction, item: discord.ui.Select, match):
        return cls(match.group("day"), list(item.options))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        values = (interaction.data or {}).get("values") or []
        task_id = values[0] if values else ""
        task = await repositories.get_task(task_id) if task_id and task_id != "none" else None
        if task is None:
            await interaction.followup.send("❌ 找不到這筆任務，可能已經結案或刪除。", ephemeral=True)
            return
        artist_map = await repositories.get_artist_map()
        now = datetime.now(config.get_tenant().tz)
        detail = task_query.task_detail_text(task, deadline_text=ui_text.deadline_text(task, now), body=task.description)
        names = "、".join(_names(task, artist_map)) or "（未關聯藝人）"
        extra = f"\n\n藝人：{names}" + (f"\n🔗 Notion：{task.url}" if task.url else "")
        embed = discord.Embed(description=(detail + extra)[:4000], color=0x5865F2)
        embed.set_footer(text="再選別筆會換成那一筆")
        options = [
            discord.SelectOption(label=o.label, value=o.value, description=o.description, default=o.value == task_id)
            for o in self.options
        ]
        view = discord.ui.View(timeout=None)
        view.add_item(OpsDetailSelect(self.day, options))
        await interaction.edit_original_response(embed=embed, view=view)


def briefing_view(day: date, b: ops_briefing.OpsBriefing) -> discord.ui.View | None:
    lines = b.option_lines()
    if not lines:
        return None
    options = [
        discord.SelectOption(label=line.text[:100], value=line.task_id, description=line.section[:100])
        for line in lines
    ]
    view = discord.ui.View(timeout=None)
    view.add_item(OpsDetailSelect(_day_code(day), options))
    return view


async def send_ops_briefing(client: discord.Client, now: datetime) -> bool:
    state = notification_log.shared()
    channel = channel_guard.ops_channel(client)
    if channel is None:
        return False
    b = await collect(now, state)
    previous = state.get("ops_briefing") or {}
    if previous.get("message_id"):
        try:
            await client.get_partial_messageable(int(previous["channel_id"])).get_partial_message(
                int(previous["message_id"])).delete()
        except discord.NotFound:
            pass
        except Exception:
            logger.warning("刪除前一天的營運早報失敗：%s", previous.get("message_id"))
    view = briefing_view(now.date(), b)
    kwargs = {"content": ops_briefing.briefing_text(now.date(), b)}
    if view is not None:
        kwargs["view"] = view
    message = await channel_guard.try_send(channel, **kwargs)
    if message is None:
        return False
    state["ops_briefing"] = {"day": _day_code(now.date()), "channel_id": message.channel.id, "message_id": message.id}
    ops_briefing.clear_queues(state)
    ops_briefing.prune_unread(state, {_day_code(now.date()), _day_code(now.date() - timedelta(days=1))})
    notification_log.persist()
    return True
