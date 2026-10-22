"""背景提醒：月度時間池催收、任務截止提醒與逾期通知。"""
import logging
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks

import config
from cogs import channel_guard, ui_text
from services import (
    audit,
    daily_briefing,
    daily_runs,
    ops_briefing,
    ops_notice,
    daily_nag,
    drive_api,
    drive_health,
    notification_log,
    repositories,
    scheduling_sla,
    task_notifications,
    teacher_reminder,
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
            # 批次重送排在派發前面：同一筆剛勾 Discord 顯示又勾重送通知時，只送一次（2026-10-22 UAT），
            # 重送送完會寫 Discord 通知時間，派發那步就會略過它
            await self._check_batch_resend()
            await self._check_task_dispatch_notifications()
            await self._check_task_assignment_notifications()
            await self._check_unacknowledged_task_notifications()
            await self._check_drive_health()
            await self._check_daily_briefing()
            await self._check_ops_briefing()
            await self._check_daily_nags()
            await self._check_teacher_post_class()
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
        channel = channel_guard.ops_channel(self.bot)
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
                logger.exception("時段收集提醒 DM 失敗：%s", artist.name)
        return sent

    async def _check_monthly_pool_deadlines(self):
        collections = time_collection.active_collections()
        if not collections:
            return
        now = datetime.now(config.TZ)
        admin_channel = channel_guard.ops_channel(self.bot)
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
                    if admin_channel and not config.get_tenant().ops_briefing_enabled:
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
                    if admin_channel and not config.get_tenant().ops_briefing_enabled:
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
        from cogs.tasks_artist import send_task_notifications

        # 這一輪批次重送剛送過的不再送（Notion 查詢可能還沒反映剛寫的 Discord 通知時間）
        resent = getattr(self, "_resent_this_round", set())
        dispatchable = [task for task in unseen if task.discord_visible and task.page_id not in resent]
        # 整輪一起送：同一位藝人這一輪有好幾筆就合併成一則（M3-27）
        outcomes = await send_task_notifications(self.bot, dispatchable) if dispatchable else {}
        for task in dispatchable:
            outcome = outcomes.get(task.page_id)
            if outcome is None:
                if task.page_id not in self._dispatch_warning_sent:
                    admin_channel = channel_guard.ops_channel(self.bot)
                    if admin_channel:
                        from cogs.ops_actions import ops_action_view

                        await channel_guard.try_send(
                            admin_channel,
                            content=ops_notice.action_title(f"任務通知送不出去：{task.name}") + "\n"
                            "找不到有效的關聯藝人／相關同仁或 Discord User ID，請回 Notion 補齊後再使用 `/任務通知重送`。"
                            + (f"\n🔗 {task.url}" if task.url else ""),
                            view=ops_action_view(),
                        )
                    self._dispatch_warning_sent.add(task.page_id)
                    await audit.log_event(
                        event_name=f"任務通知缺少收件人｜{task.name}",
                        action_type="提醒",
                        operator="System",
                        target_db="Tasks",
                        target_url=task.url,
                        after="找不到有效的關聯藝人／相關同仁或 Discord User ID",
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
                    after=f"通知 {outcome.sent} 位藝人、{outcome.staff_sent} 位同仁",
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
        if not tasks_to_notify:
            return
        from cogs.tasks_artist import send_task_notifications
        from services import task_bundle

        # 同一位藝人這一輪開放了好幾筆，就合併成一則「📚 課後作業通知」（M3-27）
        outcomes = await send_task_notifications(self.bot, tasks_to_notify, kind=task_bundle.KIND_ASSIGNMENT)
        for task in tasks_to_notify:
            outcome = outcomes.get(task.page_id)
            if outcome is None or not outcome.notifyable_artists:
                continue
            sent = outcome.sent
            if sent == len(outcome.notifyable_artists):
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
        """依 TASK_ACK_REMINDER_HOURS 掃描一次，重新推送所有尚未確認的任務。

        每日早報開著時不跑（M3-24）：沒按收到的任務改列在早報的「還沒按收到」，不再每 12 小時發新訊息。
        """
        if config.get_tenant().daily_briefing_enabled:
            return
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
        return channel_guard.ops_channel(self.bot)

    async def _check_batch_resend(self):
        """批次重送（M3-22）：Notion 勾了「重送通知」的任務，重送給藝人並取消勾選。

        先取消勾選再送：送到一半出錯時也不會每輪無限重送。已結案或沒開 Discord 顯示的略過。
        """
        self._resent_this_round = set()
        flagged = await repositories.get_tasks_flagged_for_resend()
        if not flagged:
            return  # None＝欄位還沒建；空清單＝沒人勾
        from cogs.tasks_artist import send_task_notifications

        results: list[str] = []
        ready: list[repositories.TaskItem] = []
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
            except Exception:
                logger.exception("批次重送失敗：%s", task.name)
                results.append(f"❌ {task.name}：重送失敗，請稍後再勾一次")
                continue
            ready.append(task)
        # 一起送：同一位藝人重送好幾筆就合併成一則，舊卡先刪掉（M3-27）
        try:
            outcomes = await send_task_notifications(self.bot, ready) if ready else {}
        except Exception:
            logger.exception("批次重送失敗")
            outcomes = {}
            results += [f"❌ {task.name}：重送失敗，請稍後再勾一次" for task in ready]
            ready = []
        for task in ready:
            outcome = outcomes.get(task.page_id)
            if outcome is None:
                results.append(f"❌ {task.name}：找不到有效的關聯藝人或 Discord User ID")
                continue
            self._resent_this_round.add(task.page_id)
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

    async def _check_teacher_post_class(self):
        """課後提醒負責老師到論壇發布作業要求與評語（M5-01），結案就停，最多催 N 天。"""
        tenant = config.get_tenant()
        now = datetime.now(tenant.tz)
        state = notification_log.shared()
        records = state.setdefault("teacher_reminders", {})
        tasks = await repositories.get_active_tasks()
        actions = []
        for task in tasks:
            action = teacher_reminder.next_action(
                task, now, records.get(task.page_id),
                after_hours=tenant.teacher_reminder_after_hours,
                daily_hour=tenant.daily_nag_hour,
                max_days=tenant.teacher_reminder_max_days,
            )
            if action:
                actions.append((task, action))
        if not actions:
            return
        artist_map = await repositories.get_artist_map()
        admin_channel = self._admin_channel()
        # 同一堂課在 Notion 是每位學生各一筆任務（同名、同時間、同老師），老師只該收到一則。
        groups: dict[tuple, list] = {}
        for task, action in actions:
            key = (action, task.name, teacher_reminder.class_ended_at(task), tuple(sorted(task.teacher_page_ids)))
            groups.setdefault(key, []).append(task)
        remind_groups: list[tuple] = []
        for (action, _, _, teacher_ids), group in groups.items():
            task = group[0]
            counts = []
            for item in group:
                record = records.setdefault(item.page_id, {"count": 0})
                record["last_date"] = now.date().isoformat()
                if action == "escalate":
                    record["escalated"] = True
                else:
                    record["count"] = int(record.get("count", 0)) + 1
                counts.append(int(record.get("count", 0)))
            notification_log.persist()
            teachers = await repositories.get_people_items_by_ids(list(teacher_ids))
            teacher_names = [t.name for t in teachers]
            if action == "escalate":
                if tenant.ops_briefing_enabled:
                    ops_briefing.add_teacher_note(state, teacher_reminder.escalation_note(task, teacher_names, max(counts)))
                    notification_log.persist()
                else:
                    await channel_guard.try_send(
                        admin_channel,
                        content=teacher_reminder.escalation_message(task, teacher_names, max(counts)),
                    )
                continue
            artist_names = list(dict.fromkeys(
                artist_map[p].name for item in group for p in item.artist_page_ids if p in artist_map
            ))
            remind_groups.append((group, task, teachers, teacher_names, artist_names, max(counts)))

        # 每位老師這一輪只收一則（M3-25 第三階段）：原本一堂課一則，一位老師有好幾堂就連收好幾則。
        per_teacher: dict[str, tuple] = {}
        for index, (_, task, teachers, _, artist_names, count) in enumerate(remind_groups):
            for teacher in teachers:
                if teacher.discord_user_id:
                    per_teacher.setdefault(teacher.page_id, (teacher, []))[1].append((index, task, artist_names, count))
        reached: dict[int, int] = {}
        for teacher, entries in per_teacher.values():
            message = (
                teacher_reminder.teacher_message(entries[0][1], entries[0][2]) if len(entries) == 1
                else teacher_reminder.teacher_digest_message([(task, names, count) for _, task, names, count in entries])
            )
            try:
                user = await self.bot.fetch_user(int(teacher.discord_user_id))
                await user.send(message)
                for index, *_ in entries:
                    reached[index] = reached.get(index, 0) + 1
            except Exception:
                logger.exception("課後提醒老師失敗：%s", teacher.name)
        for index, (group, task, teachers, teacher_names, artist_names, count) in enumerate(remind_groups):
            sent = reached.get(index, 0)
            if not sent:
                if tenant.ops_briefing_enabled:
                    ops_briefing.add_teacher_note(state, teacher_reminder.unreachable_note(task, teacher_names))
                    notification_log.persist()
                else:
                    await channel_guard.try_send(
                        admin_channel, content=teacher_reminder.unreachable_message(task, teacher_names)
                    )
            for item in group:
                await audit.log_event(
                    event_name=f"課後提醒老師｜{item.name}",
                    action_type="提醒", operator="System", source="System",
                    target_db="Tasks", target_url=item.url,
                    after=f"第 {records[item.page_id]['count']} 次｜私訊 {sent} 位老師"
                    + ("" if sent else "｜已轉控方頻道") + f"｜同堂 {len(group)} 筆合併一則",
                )

    async def _check_drive_health(self):
        """每日 Drive 健康檢查：讀一次總資料夾，失敗當天通知控方一次，恢復時也說一聲。"""
        tenant = config.get_tenant()
        if not tenant.drive_root_folder_id or not drive_api.is_configured():
            return
        now = datetime.now(tenant.tz)
        state = notification_log.shared()
        if not drive_health.check_due(now, state.get(drive_health.LAST_DATE_KEY), tenant.daily_nag_hour):
            return
        state[drive_health.LAST_DATE_KEY] = now.date().isoformat()
        notification_log.persist()

        # 每天從 token.json 重建一次連線：重新授權後不重啟 bot，最晚隔天也會換上新授權。
        drive_api.drop_cached_service()
        try:
            await drive_api.check_folder_access(tenant.drive_root_folder_id)
        except Exception as exc:
            logger.exception("Drive 健康檢查失敗")
            drive_api.drop_cached_service()
            message = await channel_guard.try_send(
                channel_guard.ops_channel(self.bot),
                content=drive_health.failure_message(drive_health.classify_error(exc), str(exc)),
            )
            state[drive_health.FAILING_KEY] = (
                {"channel_id": message.channel.id, "message_id": message.id, "since": now.strftime("%m/%d")}
                if message is not None else True
            )
            notification_log.persist()
            return
        failing = state.pop(drive_health.FAILING_KEY, False)
        if failing:
            notification_log.persist()
            # 恢復時把失敗那則改成綠色，不另發一則（M3-25）；找不到原訊息才另發
            edited = False
            if isinstance(failing, dict) and failing.get("message_id"):
                try:
                    partial = self.bot.get_partial_messageable(int(failing["channel_id"]))
                    await partial.get_partial_message(int(failing["message_id"])).edit(
                        content=drive_health.recovered_message(failing.get("since", ""))
                    )
                    edited = True
                except Exception:
                    logger.warning("Drive 恢復：原本的失敗訊息改不了，改另發一則")
            if not edited:
                await channel_guard.try_send(
                    channel_guard.ops_channel(self.bot),
                    content=drive_health.RECOVERED_MESSAGE,
                )
        logger.info("Drive 健康檢查正常")

    async def _check_daily_briefing(self):
        """每日早報（M3-24）：每位藝人每天一則，取代逐則的事前提醒、藝人端每日催辦與未確認重送。"""
        tenant = config.get_tenant()
        if not tenant.daily_briefing_enabled:
            return
        now = datetime.now(tenant.tz)
        state = notification_log.shared()
        job = "daily_briefing"
        if not daily_briefing.briefing_due(now, None, tenant.daily_briefing_hour):
            return
        if not daily_runs.should_run(state, job, now, state.get("briefing_last_date")):
            return
        # M3-11：全部送達才算今天完成；有人失敗下一輪重試（今天已收到的人會跳過，不重複收）
        attempt = daily_runs.begin(state, job, now)
        notification_log.persist()
        from cogs.briefing import send_daily_briefings

        try:
            stats = await send_daily_briefings(self.bot, now, final=daily_runs.is_last_attempt(state, job, now))
        except Exception:
            logger.exception("每日早報失敗（第 %s 次），下一輪重試", attempt)
            daily_runs.finish(state, job, now, complete=False, note="讀取資料失敗")
            notification_log.persist()
            return
        done = daily_runs.finish(state, job, now, complete=not stats["failed"], note=f"失敗 {stats['failed']}")
        if done:
            state["briefing_last_date"] = now.date().isoformat()
        notification_log.persist()
        await audit.log_event(
            event_name="每日早報",
            action_type="提醒", operator="System", source="System", target_db="Tasks",
            after=(f"第 {attempt} 次｜發出 {stats['sent']} 則｜失敗 {stats['failed']} 位｜列入 {stats['tasks']} 筆任務"
                   f"｜沒有任務而清掉舊早報 {stats['cleared']} 位" + ("" if done else "｜下一輪重試")),
        )

    async def _check_ops_briefing(self):
        """控方營運早報（M3-25）：每天一則，取代逾期／修改期限逐筆通知、時間池進度、發文待確認等。"""
        tenant = config.get_tenant()
        if not tenant.ops_briefing_enabled:
            return
        now = datetime.now(tenant.tz)
        state = notification_log.shared()
        job = "ops_briefing"
        if not ops_briefing.briefing_due(now, None, tenant.ops_briefing_hour, tenant.ops_briefing_minute):
            return
        if not daily_runs.should_run(state, job, now, state.get("ops_briefing_last_date")):
            return
        attempt = daily_runs.begin(state, job, now)
        notification_log.persist()
        from cogs.ops_briefing import send_ops_briefing

        try:
            sent = await send_ops_briefing(self.bot, now)
        except Exception:
            logger.exception("營運早報失敗（第 %s 次），下一輪重試", attempt)
            sent = False
        done = daily_runs.finish(state, job, now, complete=sent)
        if done:
            state["ops_briefing_last_date"] = now.date().isoformat()
        notification_log.persist()
        await audit.log_event(
            event_name="營運早報", action_type="提醒", operator="System", source="System",
            target_db="Tasks",
            after=f"第 {attempt} 次｜" + ("已發送" if sent else "未發送" + ("，下一輪重試" if not done else "，今天不再重試")),
        )

    async def _check_daily_nags(self):
        """每日催辦（M3-11）：已逾期／需修改催藝人，待審核催審核人員，一天一次、不帶按鈕。

        取代先前三套各自的節奏（逾期只發一次、需修改每階段一次、待審核每 12 小時），
        卡住的任務改成每天固定時間各催一次。最後執行日寫在 .state/，重啟後同一天不重跑。
        """
        tenant = config.get_tenant()
        now = datetime.now(tenant.tz)
        state = notification_log.shared()
        job = "daily_nag"
        if not daily_nag.daily_nag_due(now, None, tenant.daily_nag_hour):
            return
        if not daily_runs.should_run(state, job, now, state.get("daily_nag_last_date")):
            return
        # M3-11：全部送達才算今天完成；失敗的下一輪重試，已送達的收件人記在 delivered 不重發
        attempt = daily_runs.begin(state, job, now)
        notification_log.persist()
        try:
            failed = await self._send_daily_nags(now, state, job)
        except Exception:
            logger.exception("每日催辦失敗（第 %s 次），下一輪重試", attempt)
            daily_runs.finish(state, job, now, complete=False, note="讀取資料失敗")
            notification_log.persist()
            return
        if daily_runs.finish(state, job, now, complete=not failed, note=f"失敗 {failed}"):
            state["daily_nag_last_date"] = now.date().isoformat()
        notification_log.persist()

    async def _send_daily_nags(self, now: datetime, state: dict, job: str) -> int:
        """發每日催辦，回傳失敗的收件人數。已送達的（delivered）跳過。"""
        tenant = config.get_tenant()
        done_keys = daily_runs.delivered(state, job, now)
        last_attempt = daily_runs.is_last_attempt(state, job, now)
        failed = 0
        tasks = await repositories.get_active_tasks()
        artist_map = await repositories.get_artist_map()
        artist_sent = 0
        # 每日早報開著時，已逾期／需修改改在早報裡列（M3-24），這裡只留審核人員催辦。
        artist_nags = [] if tenant.daily_briefing_enabled else daily_nag.group_artist_nags(tasks, artist_map)
        for nag in artist_nags:
            key = f"artist:{nag.artist.page_id}"
            if key in done_keys:
                continue
            try:
                user = await self.bot.fetch_user(int(nag.artist.discord_user_id))
                await user.send(daily_nag.artist_nag_message(nag))
                artist_sent += 1
                daily_runs.mark_delivered(state, job, now, key)
                notification_log.persist()
            except Exception:
                logger.exception("每日催辦（藝人）失敗：%s", nag.artist.name)
                failed += 1

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
            key = f"reviewer:{pid}"
            if key in done_keys:
                continue
            try:
                user = await self.bot.fetch_user(int(reviewers[pid].discord_user_id))
                await user.send(daily_nag.review_nag_message(entries, after_hours, "⏰ **每日待審核提醒**"))
                reviewer_sent += 1
                daily_runs.mark_delivered(state, job, now, key)
                notification_log.persist()
            except Exception:
                logger.exception("每日催辦（審核人員）失敗：%s", reviewers[pid].name)
                failed += 1
                if last_attempt:
                    unassigned += entries  # 重試用完還私訊不到，就退回控方頻道，不讓提交沒人知道
        if unassigned and not tenant.ops_briefing_enabled and "ops_channel" not in done_keys:
            posted = await channel_guard.try_send(
                self._admin_channel(),
                content=daily_nag.review_nag_message(
                    unassigned, after_hours, "⏰ **待審核提醒（未指定或聯絡不到審核人員）**"
                ),
            )
            if posted is not None:
                daily_runs.mark_delivered(state, job, now, "ops_channel")
        if artist_sent or reviewer_sent or unassigned or failed:
            await audit.log_event(
                event_name="每日催辦",
                action_type="提醒", operator="System", source="System",
                target_db="Tasks & Submissions",
                after=f"藝人 {artist_sent} 位｜審核人員 {reviewer_sent} 位｜控方頻道 {len(unassigned)} 筆｜失敗 {failed} 位",
            )
        return failed

    async def _check_publish_confirm_digest(self):
        """發文類停在「已完成」的任務，彙整成一則提醒控方確認實際發布（M2-05）。

        營運早報開著時不另發（M3-25）：在早報的「發文待確認發布」那一區列出。
        """
        tenant = config.get_tenant()
        if tenant.ops_briefing_enabled:
            return
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
            admin_channel = channel_guard.ops_channel(self.bot)
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
        營運早報開著時不逐筆發（M3-25）：早報的「修改期限已過」每天列出。
        """
        if config.get_tenant().ops_briefing_enabled:
            return
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
                admin_channel,
                content=f"🚨 **修改期限逾時**｜{task.name}｜期限：{deadline_text}" + (f"\n🔗 {task.url}" if task.url else ""),
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
        staff_ids = sorted({pid for t, _, _ in due if t.discord_visible for pid in t.staff_page_ids})
        staff_map = {
            p.page_id: p for p in await repositories.get_people_items_by_ids(staff_ids)
        } if staff_ids else {}
        admin_channel = channel_guard.ops_channel(self.bot)

        for t, stages, deadline in due:
            for stage in stages:  # 一次標記全部，補跑不重複發
                if stage != repositories.OVERDUE_STAGE:
                    notification_log.mark_reminder_sent(log_state, t.page_id, stage, deadline, now)
            notification_log.persist()
            is_overdue = repositories.OVERDUE_STAGE in stages
            deadline_text = ui_text.deadline_text(t, now)
            artists = [artist_map[pid] for pid in t.artist_page_ids if pid in artist_map]

            briefing_mode = config.get_tenant().daily_briefing_enabled
            if briefing_mode and t.discord_visible and not is_overdue:
                # M3-24：到期的提醒階段排進明早的早報，不再一則一則私訊藝人。
                daily_briefing.queue_stages(log_state, t.page_id, stages, now)
                notification_log.persist()

            # DM 藝人（僅 Discord 顯示的任務；內部行政任務不打擾藝人）
            if t.discord_visible:
                from cogs.tasks_artist import notify_staff, reminder_view

                staff = [staff_map[pid] for pid in t.staff_page_ids if pid in staff_map]

                # 同一輪可能同時到期多個階段（bot 停機補跑），每個階段各發一則、各一顆按鈕，
                # 藝人才分得出自己確認的是哪一次。逾期只有一種訊息、沒有按鈕。
                for stage in ([repositories.OVERDUE_STAGE] if is_overdue else stages):
                    sent_count = 0
                    overdue_text = (
                        f"🔴 任務已逾期：**{t.name}**（{deadline_text}）\n"
                        f"請盡快處理；若已完成請提交或聯絡營運。"
                    )
                    for a in artists:
                        if not a.discord_user_id or briefing_mode:
                            # 早報模式：逾期與事前提醒都改在早報列（逾期仍會改狀態並通知控方）
                            continue
                        try:
                            user = await self.bot.fetch_user(int(a.discord_user_id))
                            if is_overdue:
                                await user.send(overdue_text)
                            else:
                                await user.send(
                                    task_notifications.reminder_message(t, stage, deadline_text),
                                    view=reminder_view(t.page_id, stage),
                                )
                            sent_count += 1
                        except Exception:
                            logger.exception("任務提醒 DM 失敗：%s／%s", t.name, a.name)
                    if staff and not briefing_mode:
                        # M6-05：同仁也收同一則提醒（沒有確認鈕，也不算進「已發送給 N 人」）。
                        # 早報模式（M3-25 第三階段）：同仁改收每天一則同仁早報。
                        await notify_staff(
                            self.bot, t,
                            overdue_text if is_overdue
                            else task_notifications.reminder_message(t, stage, deadline_text),
                            staff,
                        )
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
                if config.get_tenant().ops_briefing_enabled:
                    ops_briefing.record_new_overdue(log_state, t.page_id, now)  # 早報列「新逾期」（M3-25）
                    notification_log.persist()
                elif admin_channel:
                    await admin_channel.send(
                        f"🚨 任務逾期：**{t.name}**｜{names}｜{deadline_text}" + (f"\n🔗 {t.url}" if t.url else "")
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
