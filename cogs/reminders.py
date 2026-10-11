"""背景提醒：月度時間池催收、任務截止提醒與逾期通知。"""
import logging
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks

import config
from cogs import channel_guard, ui_text
from services import (
    audit,
    daily_nag,
    notification_log,
    repositories,
    scheduling_sla,
    task_notifications,
    time_collection,
)

logger = logging.getLogger(__name__)


def review_reminder_due(
    created_at: datetime | None,
    now: datetime,
    after_hours: float,
) -> bool:
    """判斷待審核提交是否已達首次催辦門檻。"""
    if created_at is None:
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=config.TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=config.TZ)
    return now >= created_at.astimezone(config.TZ) + timedelta(hours=after_hours)


class RemindersCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 每筆需求已送過的提醒階段（in-memory，重啟後最多重發一次，可接受）
        self._sent: dict[str, set[str]] = {}
        self._task_dispatch_state = task_notifications.load_state()
        self._assignment_notification_state = task_notifications.load_assignment_state()
        self._dispatch_warning_sent: set[str] = set()
        # 啟動時先建立掃描基準，避免啟動 Bot 的同一輪重複派送初始通知。
        self._last_unacknowledged_scan_at = datetime.now(config.get_tenant().tz)
        self.check_loop.start()

    def cog_unload(self):
        self.check_loop.cancel()

    def _once(self, request_id: str, stage: str) -> bool:
        """同一需求同一階段只提醒一次。回傳 True 表示本次應該發送。"""
        stages = self._sent.setdefault(request_id, set())
        if stage in stages:
            return False
        stages.add(stage)
        return True

    @tasks.loop(minutes=config.REMINDER_INTERVAL_MINUTES)
    async def check_loop(self):
        try:
            await self._check_monthly_pool_deadlines()
            await self._check_scheduling_sla()
            await self._check_task_dispatch_notifications()
            await self._check_task_assignment_notifications()
            await self._check_unacknowledged_task_notifications()
            await self._check_batch_resend()
            await self._check_daily_nags()
            await self._check_revision_deadlines()
            await self._check_task_reminders()
            await self._check_publish_confirm_digest()
        except Exception:
            logger.exception("提醒 loop 執行失敗")

    @check_loop.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    async def _monthly_pool_progress(
        self,
        collection: time_collection.ActiveCollection,
    ) -> tuple[
        list[repositories.Artist],
        list[repositories.Artist],
        repositories.MonthlyPoolProgress,
    ]:
        artists = await repositories.get_artists_by_ids(list(collection.artist_page_ids))
        artist_by_id = {artist.page_id: artist for artist in artists}
        progress = await repositories.get_monthly_pool_progress(
            collection.month,
            list(collection.artist_page_ids),
        )
        missing = [
            artist_by_id[page_id] for page_id in progress.missing_artist_page_ids
            if page_id in artist_by_id
        ]
        return artists, missing, progress

    async def _check_scheduling_sla(self):
        channel = (
            self.bot.get_channel(config.ADMIN_NOTIFY_CHANNEL_ID)
            or self.bot.get_channel(config.SCHEDULE_CHANNEL_ID)
        )
        if channel is None:
            return
        for event in scheduling_sla.due_events(int(datetime.now(config.TZ).timestamp())):
            try:
                await channel.send(event.message)
            except Exception:
                logger.exception("排課 SLA 通知發送失敗（event=%s）", event.key)
                continue
            scheduling_sla.mark_event_sent(event.key)
            await audit.log_event(
                event_name=f"排課 SLA 提醒｜{event.key}",
                action_type="提醒",
                operator="System",
                source="System",
                target_db="Tasks",
                after=event.message,
            )

    async def _dm_monthly_pool_missing(
        self,
        artists: list[repositories.Artist],
        month: str,
        deadline: datetime,
        overdue: bool = False,
    ) -> int:
        sent = 0
        for artist in artists:
            if not artist.discord_user_id:
                continue
            try:
                user = await self.bot.fetch_user(int(artist.discord_user_id))
                if overdue:
                    await user.send(
                        f"🔴 **{month} 月度可配合時段已截止**，你還沒有完成回填。\n"
                        "請盡快回到 Discord 公告填寫，或直接聯絡營運。"
                    )
                else:
                    await user.send(
                        f"⏰ **{month} 月度可配合時段即將截止**"
                        f"（{deadline.strftime('%m/%d %H:%M')}）。\n"
                        "請回到 Discord 公告填寫可配合時間。"
                    )
                sent += 1
            except Exception:
                pass
        return sent

    async def _check_monthly_pool_deadlines(self):
        collections = time_collection.active_collections()
        if not collections:
            return
        now = datetime.now(config.TZ)
        admin_channel = self.bot.get_channel(config.ADMIN_NOTIFY_CHANNEL_ID) \
            or self.bot.get_channel(config.SCHEDULE_CHANNEL_ID)
        for collection in collections:
            deadline = datetime.fromtimestamp(collection.deadline_ts, tz=config.TZ)
            artists, missing, progress = await self._monthly_pool_progress(collection)
            if progress.is_complete:
                if time_collection.collection_notice_once(
                    collection.month, collection.deadline_ts, "all_done"
                ) and admin_channel:
                    names = "、".join(artist.name for artist in artists) or "（無）"
                    await admin_channel.send(
                        f"✅ **{collection.month} 月度時間池已收齊**\n"
                        f"已填藝人：{names}\n"
                        f"可以開始使用 `/從時間池建立任務 月份:{collection.month} 時長分鐘:60` 查看共同空檔。"
                    )
                # 終態：之後不再對這個收集做每輪查詢
                time_collection.deregister_collection(collection.month, collection.deadline_ts)
                continue
            if not missing:
                continue
            names = "、".join(artist.name for artist in missing)
            mentions = " ".join(f"<@{artist.discord_user_id}>" for artist in missing if artist.discord_user_id)
            if now < deadline <= now + timedelta(hours=config.REMIND_BEFORE_DEADLINE_HOURS):
                if time_collection.collection_notice_once(
                    collection.month, collection.deadline_ts, "pre_deadline"
                ):
                    dm_sent = await self._dm_monthly_pool_missing(missing, collection.month, deadline)
                    if admin_channel:
                        await admin_channel.send(
                            f"⏰ **{collection.month} 月度時間池催填**\n"
                            f"未填：{names}\n"
                            f"已 DM push：{dm_sent}/{len(missing)} 位"
                            + (f"\n{mentions}" if mentions else "")
                        )
                    await audit.log_event(
                        event_name=f"月度時間池截止前催填｜{collection.month}",
                        action_type="提醒", operator="System", source="System",
                        target_db="Availability Slots",
                        after=f"未填：{names}",
                    )
            elif now > deadline:
                if time_collection.collection_notice_once(
                    collection.month, collection.deadline_ts, "post_deadline"
                ):
                    dm_sent = await self._dm_monthly_pool_missing(
                        missing, collection.month, deadline, overdue=True
                    )
                    if admin_channel:
                        await admin_channel.send(
                            f"🔴 **{collection.month} 月度時間池已截止，仍有人未填**\n"
                            f"未填：{names}\n"
                            f"已 DM push：{dm_sent}/{len(missing)} 位"
                            + (f"\n{mentions}" if mentions else "")
                        )
                    await audit.log_event(
                        event_name=f"月度時間池截止後未填通知｜{collection.month}",
                        action_type="提醒", operator="System", source="System",
                        target_db="Availability Slots",
                        after=f"逾期未填：{names}",
                    )
                # 終態：逾期通知已發，之後不再對這個收集做每輪查詢
                time_collection.deregister_collection(collection.month, collection.deadline_ts)

    # ---------- P0-4 任務提醒：截止前提醒、逾期標記＋管理層升級 ----------

    async def _check_task_dispatch_notifications(self):
        """通知新建立且開啟 Discord 顯示的任務；首次啟用只建立基準。"""
        all_tasks = await repositories.get_active_tasks()
        stale_hidden_tasks = (
            task_notifications.find_hidden_tasks_needing_notification_reset(all_tasks)
        )
        for task in stale_hidden_tasks:
            try:
                await repositories.reset_task_discord_notification(task.page_id)
                logger.info("已重置隱藏任務的 Discord 通知狀態：%s", task.name)
            except Exception:
                logger.exception(
                    "隱藏任務通知狀態重置失敗：%s（%s）",
                    task.name,
                    task.page_id,
                )
        unseen, next_state = task_notifications.find_dispatch_tasks(
            all_tasks, self._task_dispatch_state
        )
        self._task_dispatch_state = next_state
        task_notifications.save_state(next_state)
        from cogs.tasks_artist import send_task_notification

        for task in unseen:
            if not task.discord_visible:
                continue
            outcome = await send_task_notification(self.bot, task)
            if outcome is None:
                if task.page_id not in self._dispatch_warning_sent:
                    admin_channel = (
                        self.bot.get_channel(config.ADMIN_NOTIFY_CHANNEL_ID)
                        or self.bot.get_channel(config.TASK_CHANNEL_ID)
                        or self.bot.get_channel(config.SCHEDULE_CHANNEL_ID)
                    )
                    if admin_channel:
                        await admin_channel.send(
                            f"⚠️ **任務通知未送出：{task.name}**\n"
                            "找不到有效的關聯藝人或 Discord User ID，請回 Notion 補齊後再使用 `/任務通知重送`。"
                        )
                    self._dispatch_warning_sent.add(task.page_id)
                    await audit.log_event(
                        event_name=f"任務通知缺少收件人｜{task.name}",
                        action_type="提醒",
                        operator="System",
                        target_db="Tasks",
                        target_url=task.url,
                        after="找不到有效的關聯藝人或 Discord User ID",
                    )
                continue
            self._dispatch_warning_sent.discard(task.page_id)
            if outcome.is_complete:
                await audit.log_event(
                    event_name=f"任務派發通知｜{task.name}",
                    action_type="提醒",
                    operator="System",
                    target_db="Tasks",
                    target_url=task.url,
                    after=f"通知 {outcome.sent} 位藝人",
                )
            else:
                # 私訊失敗時不要永久標記已處理，下一輪會重新嘗試。
                self._task_dispatch_state = task_notifications.forget_task(
                    task.page_id, self._task_dispatch_state
                )
                task_notifications.save_state(self._task_dispatch_state)
                logger.warning(
                    "任務派發未完成，將於下一輪重試：%s（成功 %s／應通知 %s）",
                    task.name,
                    outcome.sent,
                    len(outcome.notifyable_artists),
                )

    async def _check_task_assignment_notifications(self):
        """通知已開放提交的課後作業；每筆 Task 只通知一次。"""
        tasks_to_notify = [
            task for task in await repositories.get_active_tasks()
            if task_notifications.is_assignment_dispatchable(task)
            and not task_notifications.was_assignment_notified(
                task.page_id, self._assignment_notification_state
            )
        ]
        for task in tasks_to_notify:
            artists = await repositories.get_artists_by_ids(task.artist_page_ids)
            notifyable_artists = [artist for artist in artists if artist.discord_user_id]
            if not notifyable_artists:
                continue
            sent = 0
            for artist in notifyable_artists:
                try:
                    user = await self.bot.fetch_user(int(artist.discord_user_id))
                    from cogs.tasks_artist import send_task_dm
                    await send_task_dm(
                        user, task.page_id, task_notifications.assignment_notification_message(task)
                    )
                    sent += 1
                except Exception:
                    logger.exception("課後作業通知失敗：%s／%s", task.name, artist.name)
            if sent == len(notifyable_artists):
                self._assignment_notification_state = task_notifications.mark_assignment_notified(
                    task.page_id, self._assignment_notification_state
                )
                task_notifications.save_assignment_state(self._assignment_notification_state)
                await audit.log_event(
                    event_name=f"課後作業通知｜{task.name}",
                    action_type="提醒",
                    operator="System",
                    target_db="Tasks",
                    target_url=task.url,
                    after=f"通知 {sent} 位藝人",
                )

    async def _check_unacknowledged_task_notifications(self):
        """依 TASK_ACK_REMINDER_HOURS 掃描一次，重新推送所有尚未確認的任務。"""
        now = datetime.now(config.get_tenant().tz)
        if not task_notifications.should_run_acknowledgement_scan(
            self._last_unacknowledged_scan_at,
            now,
            config.get_tenant().task_ack_reminder_hours,
        ):
            return
        self._last_unacknowledged_scan_at = now
        tasks = await repositories.get_active_tasks()
        pending = [
            task for task in tasks
            if task.discord_visible
            and task.discord_acknowledged_at is None
        ]
        if not pending:
            return

        artist_map = await repositories.get_artist_map()
        by_artist: dict[str, list[repositories.TaskItem]] = {}
        for task in pending:
            for artist_page_id in task.artist_page_ids:
                artist = artist_map.get(artist_page_id)
                if (
                    artist
                    and artist.discord_user_id
                ):
                    by_artist.setdefault(artist_page_id, []).append(task)

        for artist_page_id, artist_tasks in by_artist.items():
            artist = artist_map[artist_page_id]
            try:
                user = await self.bot.fetch_user(int(artist.discord_user_id))
                await user.send(task_notifications.acknowledgement_reminder_message(artist_tasks))
                from cogs.tasks_artist import send_task_dm
                for task in artist_tasks:
                    await send_task_dm(user, task.page_id, task_notifications.notification_message(task))
                await audit.log_event(
                    event_name=f"任務未確認提醒｜{artist.name}",
                    action_type="提醒",
                    operator="System",
                    discord_user_id=str(artist.discord_user_id),
                    target_db="Tasks",
                    after="、".join(task.name for task in artist_tasks),
                )
                logger.info(
                    "任務未確認提醒成功：%s，%s 筆任務",
                    artist.name,
                    len(artist_tasks),
                )
            except Exception:
                logger.exception("任務未確認提醒失敗：%s", artist.name)

    def _admin_channel(self):
        return (
            self.bot.get_channel(config.ADMIN_NOTIFY_CHANNEL_ID)
            or self.bot.get_channel(config.TASK_CHANNEL_ID)
            or self.bot.get_channel(config.SCHEDULE_CHANNEL_ID)
        )

    async def _check_batch_resend(self):
        """批次重送（M3-22）：Notion 勾了「重送通知」的任務，重送給藝人並取消勾選。

        先取消勾選再送：送到一半出錯時也不會每輪無限重送。已結案或沒開 Discord 顯示的略過。
        """
        flagged = await repositories.get_tasks_flagged_for_resend()
        if not flagged:
            return  # None＝欄位還沒建；空清單＝沒人勾
        from cogs.tasks_artist import send_task_notification

        results: list[str] = []
        for task in flagged:
            try:
                await repositories.clear_task_resend_flag(task.page_id)
            except Exception:
                logger.exception("取消重送勾選失敗，這輪先不送以免重複：%s", task.name)
                continue
            if not task.discord_visible or task.status in repositories.INACTIVE_TASK_STATUSES:
                results.append(f"⏭️ {task.name}：未開 Discord 顯示或已結案，略過")
                continue
            try:
                await repositories.reset_task_discord_notification(task.page_id)
                outcome = await send_task_notification(self.bot, task)
            except Exception:
                logger.exception("批次重送失敗：%s", task.name)
                results.append(f"❌ {task.name}：重送失敗，請稍後再勾一次")
                continue
            if outcome is None:
                results.append(f"❌ {task.name}：找不到有效的關聯藝人或 Discord User ID")
                continue
            results.append(f"✅ {task.name}：已重送 {outcome.sent}／{len(outcome.notifyable_artists)} 位")
            await audit.log_event(
                event_name=f"批次重送任務通知｜{task.name}",
                action_type="更新", operator="System（Notion 勾選）", source="System",
                target_db="Tasks", target_url=task.url,
                after=f"重新通知 {outcome.sent}／{len(outcome.notifyable_artists)} 位藝人",
            )
        if results:
            await channel_guard.try_send(
                self._admin_channel(),
                content="🔁 **批次重送結果**\n" + "\n".join(results[:30]),
            )

    async def _check_daily_nags(self):
        """每日催辦（M3-11）：已逾期／需修改催藝人，待審核催審核人員，一天一次、不帶按鈕。

        取代先前三套各自的節奏（逾期只發一次、需修改每階段一次、待審核每 12 小時），
        卡住的任務改成每天固定時間各催一次。最後執行日寫在 .state/，重啟後同一天不重跑。
        """
        tenant = config.get_tenant()
        now = datetime.now(tenant.tz)
        state = notification_log.shared()
        if not daily_nag.daily_nag_due(now, state.get("daily_nag_last_date"), tenant.daily_nag_hour):
            return
        # 先記下今天已跑，再發送：中途失敗也不會在同一天每 30 分鐘重試轟炸一次。
        state["daily_nag_last_date"] = now.date().isoformat()
        notification_log.persist()

        tasks = await repositories.get_active_tasks()
        artist_map = await repositories.get_artist_map()
        artist_sent = 0
        for nag in daily_nag.group_artist_nags(tasks, artist_map):
            try:
                user = await self.bot.fetch_user(int(nag.artist.discord_user_id))
                await user.send(daily_nag.artist_nag_message(nag))
                artist_sent += 1
            except Exception:
                logger.exception("每日催辦（藝人）失敗：%s", nag.artist.name)

        after_hours = tenant.review_reminder_after_hours
        submissions = await repositories.get_submissions_by_review_status(["待審核"])
        tasks_by_id = {task.page_id: task for task in tasks}
        pending = daily_nag.pending_review_tasks(
            submissions, tasks_by_id, now, after_hours, review_reminder_due
        )
        reviewer_ids = sorted({pid for tid in pending for pid in tasks_by_id[tid].reviewer_page_ids})
        reviewers = {p.page_id: p for p in await repositories.get_people_by_ids(reviewer_ids)}
        per_reviewer: dict[str, list] = {}
        unassigned: list = []
        for task_id, subs in pending.items():
            task = tasks_by_id[task_id]
            entries = [(task, sub) for sub in subs]
            reachable = [
                pid for pid in task.reviewer_page_ids
                if pid in reviewers and reviewers[pid].discord_user_id
            ]
            if not reachable:
                unassigned += entries
            for pid in reachable:
                per_reviewer.setdefault(pid, []).extend(entries)
        reviewer_sent = 0
        for pid, entries in per_reviewer.items():
            try:
                user = await self.bot.fetch_user(int(reviewers[pid].discord_user_id))
                await user.send(daily_nag.review_nag_message(entries, after_hours, "⏰ **每日待審核提醒**"))
                reviewer_sent += 1
            except Exception:
                logger.exception("每日催辦（審核人員）失敗：%s", reviewers[pid].name)
                unassigned += entries  # 私訊不到就退回控方頻道，不讓提交沒人知道
        if unassigned:
            await channel_guard.try_send(
                self._admin_channel(),
                content=daily_nag.review_nag_message(
                    unassigned, after_hours, "⏰ **待審核提醒（未指定或聯絡不到審核人員）**"
                ),
            )
        if artist_sent or reviewer_sent or unassigned:
            await audit.log_event(
                event_name="每日催辦",
                action_type="提醒", operator="System", source="System",
                target_db="Tasks & Submissions",
                after=f"藝人 {artist_sent} 位｜審核人員 {reviewer_sent} 位｜控方頻道 {len(unassigned)} 筆",
            )

    async def _check_publish_confirm_digest(self):
        """發文類停在「已完成」的任務，彙整成一則提醒控方確認實際發布（M2-05）。"""
        tenant = config.get_tenant()
        now = datetime.now(tenant.tz)
        state = task_notifications.load_state(task_notifications.PUBLISH_DIGEST_STATE_PATH)
        if not task_notifications.publish_confirm_digest_due(
            now,
            state.get("last_sent_on"),
            tenant.publish_confirm_digest_weekday,
            tenant.publish_confirm_digest_hour,
        ):
            return
        tasks = await repositories.get_tasks_by_status(["已完成"])
        waiting = repositories.tasks_awaiting_publish_confirmation(
            tasks, now, tenant.publish_confirm_digest_after_days
        )
        if waiting:
            artist_map = await repositories.get_artist_map()
            artist_names = {page_id: artist.name for page_id, artist in artist_map.items()}
            admin_channel = (
                self.bot.get_channel(config.ADMIN_NOTIFY_CHANNEL_ID)
                or self.bot.get_channel(config.TASK_CHANNEL_ID)
                or self.bot.get_channel(config.SCHEDULE_CHANNEL_ID)
            )
            sent = await channel_guard.try_send(
                admin_channel,
                content=task_notifications.publish_confirm_digest_message(
                    waiting, artist_names, tenant.publish_confirm_digest_after_days
                ),
            )
            if sent is None:
                return  # 沒發出去就不記已發送，下一輪再試
            await audit.log_event(
                event_name="發文待確認發布彙整",
                action_type="提醒", operator="System", source="System",
                target_db="Tasks",
                after=f"列出 {len(waiting)} 筆：" + "、".join(task.name for task in waiting[:10]),
            )
        # 沒有待確認任務也記下今天已檢查，避免整天每輪都重查 Notion
        task_notifications.save_state(
            {"last_sent_on": now.date().isoformat()},
            task_notifications.PUBLISH_DIGEST_STATE_PATH,
        )

    async def _check_revision_deadlines(self):
        """修改期限逾時通知控方，每筆每個期限只通知一次（寫檔去重，重啟不重發）。

        藝人端的需修改提醒已併進每日催辦（M3-11），這裡只剩控方的逾時升級。
        """
        tasks = await repositories.get_active_tasks()
        now = datetime.now(config.TZ)
        state = notification_log.shared()
        due = [
            task for task in tasks
            if repositories.compute_revision_deadline_stage(task, now) == "修改期限逾時"
            and not notification_log.reminder_sent(state, task.page_id, "修改期限逾時", task.revision_deadline)
        ]
        if not due:
            return
        admin_channel = self._admin_channel()
        for task in due:
            notification_log.mark_reminder_sent(state, task.page_id, "修改期限逾時", task.revision_deadline, now)
            notification_log.persist()
            deadline_text = task.revision_deadline.strftime("%m/%d %H:%M")
            await channel_guard.try_send(
                admin_channel, content=f"🚨 **修改期限逾時**｜{task.name}｜期限：{deadline_text}"
            )
            await audit.log_event(
                event_name=f"修改期限提醒｜{task.name}",
                action_type="提醒", operator="System", source="System",
                target_db="Tasks", target_url=task.url,
                after=f"修改期限逾時｜{deadline_text}",
            )

    # ---------- P0-4 任務提醒：截止前提醒、逾期標記＋管理層升級 ----------

    async def _check_task_reminders(self):
        now = datetime.now(config.TZ)
        all_tasks = await repositories.get_active_tasks()
        # 已送過的階段寫在 .state/，不放記憶體：先前只存記憶體，每重啟一次 bot，
        # 窗口內所有 D-5／D-3 就整批重發一輪（2026-10-09、10-10 實際發生，M3-23）。
        log_state = notification_log.shared()
        notification_log.prune(log_state, now)
        due: list[tuple[repositories.TaskItem, list[str], datetime | None]] = []
        for t in all_tasks:
            if t.status == "已逾期":
                continue  # 已升級過（狀態持久化在 Notion，重啟也不會重複轟炸）
            stages = repositories.compute_due_reminder_stages(t, now)
            deadline, _ = repositories.task_action_deadline(t)
            # 逾期只靠 Notion 狀態「已逾期」去重（升級後上面 continue 跳過），不寫檔也不記憶體去重：
            # 控方改回進行中但沒延期，下一輪就再通知一次並改回已逾期，行為不再取決於
            # bot 有沒有重啟過（M2-13）。只有「寫狀態失敗」時才用記憶體擋，免得每 30 分鐘轟炸。
            failed_overdue = self._sent.get(f"overdue-failed:{t.page_id}", set())

            def already_sent(stage: str) -> bool:
                if stage == repositories.OVERDUE_STAGE:
                    return deadline is not None and deadline.date().isoformat() in failed_overdue
                return notification_log.reminder_sent(log_state, t.page_id, stage, deadline)

            unsent = [s for s in stages if not already_sent(s)]
            if unsent:
                due.append((t, unsent, deadline))
        if not log_state.get("reminders_baseline_done"):
            # 第一次啟用持久化去重：窗口內的截止前提醒舊版已經發過（而且每次重啟都重發），
            # 這一輪只記成已送、不再發，避免上線當下又整批重發一次。逾期照常處理。
            for t, stages, deadline in due:
                for stage in stages:
                    if stage != repositories.OVERDUE_STAGE:
                        notification_log.mark_reminder_sent(log_state, t.page_id, stage, deadline, now)
            log_state["reminders_baseline_done"] = True
            notification_log.persist()
            due = [
                (t, [repositories.OVERDUE_STAGE], deadline)
                for t, stages, deadline in due
                if repositories.OVERDUE_STAGE in stages
            ]
        if not due:
            return

        artist_map = await repositories.get_artist_map()  # 一輪只撈一次
        admin_channel = self.bot.get_channel(config.ADMIN_NOTIFY_CHANNEL_ID) \
            or self.bot.get_channel(config.TASK_CHANNEL_ID) \
            or self.bot.get_channel(config.SCHEDULE_CHANNEL_ID)

        for t, stages, deadline in due:
            for stage in stages:  # 一次標記全部，補跑不重複發
                if stage != repositories.OVERDUE_STAGE:
                    notification_log.mark_reminder_sent(log_state, t.page_id, stage, deadline, now)
            notification_log.persist()
            is_overdue = repositories.OVERDUE_STAGE in stages
            deadline_text = ui_text.deadline_text(t, now)
            artists = [artist_map[pid] for pid in t.artist_page_ids if pid in artist_map]

            # DM 藝人（僅 Discord 顯示的任務；內部行政任務不打擾藝人）
            if t.discord_visible:
                from cogs.tasks_artist import reminder_view

                # 同一輪可能同時到期多個階段（bot 停機補跑），每個階段各發一則、各一顆按鈕，
                # 藝人才分得出自己確認的是哪一次。逾期只有一種訊息、沒有按鈕。
                for stage in ([repositories.OVERDUE_STAGE] if is_overdue else stages):
                    sent_count = 0
                    for a in artists:
                        if not a.discord_user_id:
                            continue
                        try:
                            user = await self.bot.fetch_user(int(a.discord_user_id))
                            if is_overdue:
                                await user.send(
                                    f"🔴 任務已逾期：**{t.name}**（{deadline_text}）\n"
                                    f"請盡快處理；若已完成請提交或聯絡營運。"
                                )
                            else:
                                await user.send(
                                    task_notifications.reminder_message(t, stage, deadline_text),
                                    view=reminder_view(t.page_id, stage),
                                )
                            sent_count += 1
                        except Exception:
                            pass
                    if not is_overdue and sent_count:
                        # 送出也記一行，控方才分得出「還沒發」和「發了沒人按」。
                        try:
                            await repositories.append_reminder_ack_line(
                                t.page_id,
                                task_notifications.format_reminder_sent_line(stage, now, sent_count),
                            )
                        except Exception:
                            logger.exception("提醒送出紀錄寫入失敗：%s", t.name)

            if is_overdue:
                try:
                    await repositories.update_task_status(t.page_id, "已逾期")
                except Exception:
                    logger.exception("標記任務已逾期失敗")
                    if deadline is not None:
                        self._once(f"overdue-failed:{t.page_id}", deadline.date().isoformat())
                names = "、".join(a.name for a in artists) or "（未關聯藝人）"
                if admin_channel:
                    await admin_channel.send(
                        f"🚨 任務逾期：**{t.name}**｜{names}｜{deadline_text}"
                    )
                await audit.log_event(
                    event_name=f"任務逾期升級｜{t.name}",
                    action_type="提醒", operator="System", source="System",
                    target_db="Tasks", target_url=t.url,
                    after=f"{names}｜{deadline_text}",
                )
            else:
                await audit.log_event(
                    event_name=f"任務截止提醒｜{t.name}",
                    action_type="提醒", operator="System", source="System",
                    target_db="Tasks", target_url=t.url,
                    after="、".join(stages) + f"｜{deadline_text}",
                )


async def setup(bot: commands.Bot):
    await bot.add_cog(RemindersCog(bot))
