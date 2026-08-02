"""背景提醒：月度時間池催收、任務截止提醒與逾期通知。"""
import logging
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks

import config
from cogs import ui_text
from services import audit, repositories, task_notifications, time_collection

logger = logging.getLogger(__name__)


class RemindersCog(commands.Cog):
    TASK_ACK_REMINDER_HOURS = 6

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 每筆需求已送過的提醒階段（in-memory，重啟後最多重發一次，可接受）
        self._sent: dict[str, set[str]] = {}
        self._task_dispatch_state = task_notifications.load_state()
        self._dispatch_warning_sent: set[str] = set()
        self._task_ack_reminded_at: dict[str, datetime] = {}
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
            await self._check_task_dispatch_notifications()
            await self._check_unacknowledged_task_notifications()
            await self._check_task_reminders()
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
                        f"可以開始使用 `/時間池比對 月份:{collection.month} 時長分鐘:60` 查看共同空檔。"
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
        unseen, next_state = task_notifications.find_dispatch_tasks(
            all_tasks, self._task_dispatch_state
        )
        self._task_dispatch_state = next_state
        task_notifications.save_state(next_state)
        for task in unseen:
            if not task.discord_visible:
                continue
            artists = await repositories.get_artists_by_ids(task.artist_page_ids)
            notifyable_artists = [artist for artist in artists if artist.discord_user_id]
            if not notifyable_artists:
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
            sent = 0
            for artist in artists:
                if not artist.discord_user_id:
                    continue
                try:
                    user = await self.bot.fetch_user(int(artist.discord_user_id))
                    view = discord.ui.View(timeout=None)
                    from cogs.tasks_artist import TaskReceiptButton
                    view.add_item(TaskReceiptButton(task.page_id))
                    if task.url:
                        view.add_item(
                            discord.ui.Button(
                                label="查看任務內容",
                                style=discord.ButtonStyle.link,
                                url=task.url,
                                emoji="📄",
                            )
                        )
                    await user.send(task_notifications.notification_message(task), view=view)
                    sent += 1
                except Exception:
                    logger.exception("任務派發通知失敗：%s／%s", task.name, artist.name)
            if notifyable_artists and sent == len(notifyable_artists):
                logger.info("任務派發通知成功：%s，已通知 %s 位藝人", task.name, sent)
                try:
                    await repositories.mark_task_discord_notified(task.page_id)
                except Exception:
                    logger.exception("任務通知時間寫入 Notion 失敗：%s", task.page_id)
                await audit.log_event(
                    event_name=f"任務派發通知｜{task.name}",
                    action_type="提醒",
                    operator="System",
                    target_db="Tasks",
                    target_url=task.url,
                    after=f"通知 {sent} 位藝人",
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
                    sent,
                    len(notifyable_artists),
                )

    async def _check_unacknowledged_task_notifications(self):
        """每 12 小時提醒藝人逐筆確認尚未收到的任務。"""
        now = datetime.now(config.get_tenant().tz)
        tasks = await repositories.get_active_tasks()
        pending = [
            task for task in tasks
            if task.discord_visible
            and task.discord_notified_at is not None
            and task.discord_acknowledged_at is None
            and now - task.discord_notified_at >= timedelta(hours=self.TASK_ACK_REMINDER_HOURS)
        ]
        if not pending:
            return

        artist_map = await repositories.get_artist_map()
        by_artist: dict[str, list[repositories.TaskItem]] = {}
        for task in pending:
            last_reminded = self._task_ack_reminded_at.get(task.page_id)
            if last_reminded and now - last_reminded < timedelta(hours=self.TASK_ACK_REMINDER_HOURS):
                continue
            for artist_page_id in task.artist_page_ids:
                artist = artist_map.get(artist_page_id)
                if artist and artist.discord_user_id:
                    by_artist.setdefault(artist_page_id, []).append(task)

        for artist_page_id, artist_tasks in by_artist.items():
            artist = artist_map[artist_page_id]
            try:
                user = await self.bot.fetch_user(int(artist.discord_user_id))
                await user.send(task_notifications.acknowledgement_reminder_message(artist_tasks))
                for task in artist_tasks:
                    self._task_ack_reminded_at[task.page_id] = now
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

    # ---------- P0-4 任務提醒：截止前提醒、逾期標記＋管理層升級 ----------

    async def _check_task_reminders(self):
        now = datetime.now(config.TZ)
        all_tasks = await repositories.get_active_tasks()
        due: list[tuple[repositories.TaskItem, list[str]]] = []
        for t in all_tasks:
            if t.status == "已逾期":
                continue  # 已升級過（狀態持久化在 Notion，重啟也不會重複轟炸）
            stages = repositories.compute_due_reminder_stages(t, now)
            sent = self._sent.get(f"task:{t.page_id}", set())
            unsent = [s for s in stages if s not in sent]
            if unsent:
                due.append((t, unsent))
        if not due:
            return

        artist_map = await repositories.get_artist_map()  # 一輪只撈一次
        admin_channel = self.bot.get_channel(config.TASK_CHANNEL_ID) \
            or self.bot.get_channel(config.ADMIN_NOTIFY_CHANNEL_ID) \
            or self.bot.get_channel(config.SCHEDULE_CHANNEL_ID)

        for t, stages in due:
            for stage in stages:
                self._once(f"task:{t.page_id}", stage)  # 一次標記全部，補跑不重複發
            is_overdue = repositories.OVERDUE_STAGE in stages
            deadline_text = ui_text.deadline_text(t, now)
            artists = [artist_map[pid] for pid in t.artist_page_ids if pid in artist_map]

            # DM 藝人（僅 Discord 顯示的任務；內部行政任務不打擾藝人）
            if t.discord_visible:
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
                            await user.send(f"⏰ 任務提醒：**{t.name}**｜{deadline_text}")
                    except Exception:
                        pass

            if is_overdue:
                try:
                    await repositories.update_task_status(t.page_id, "已逾期")
                except Exception:
                    logger.exception("標記任務已逾期失敗")
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
