"""背景提醒：填寫截止催收、逾期通知管理者、2 天 SLA 升級。"""
import logging
from datetime import datetime, timedelta

from discord.ext import commands, tasks

import config
from cogs import tasks_artist
from services import audit, repositories

logger = logging.getLogger(__name__)


class RemindersCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 每筆需求已送過的提醒階段（in-memory，重啟後最多重發一次，可接受）
        self._sent: dict[str, set[str]] = {}
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
            await self._check_fill_deadlines()
            await self._check_sla()
            await self._check_task_reminders()
        except Exception:
            logger.exception("提醒 loop 執行失敗")

    @check_loop.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    async def _unfilled_artists(
        self,
        request: repositories.ScheduleRequest,
        artist_map: dict[str, repositories.Artist],
    ) -> list[repositories.Artist]:
        slots = await repositories.get_slots_for_request(request.page_id)
        filled_ids = {pid for s in slots for pid in s.artist_page_ids}
        return [
            artist_map[pid] for pid in request.artist_page_ids
            if pid in artist_map and pid not in filled_ids
        ]

    async def _check_fill_deadlines(self):
        now = datetime.now(config.TZ)
        requests = await repositories.get_requests_by_status(["等待藝人填寫"])
        if not requests:
            return
        artist_map = await repositories.get_artist_map()  # 一輪只撈一次藝人表
        channel = self.bot.get_channel(config.SCHEDULE_CHANNEL_ID)
        admin_channel = self.bot.get_channel(config.ADMIN_NOTIFY_CHANNEL_ID) or channel

        for r in requests:
            if not r.fill_deadline:
                continue
            unfilled = await self._unfilled_artists(r, artist_map)
            if not unfilled:
                continue
            names = "、".join(a.name for a in unfilled)

            # 截止前 N 小時：頻道點名 + DM
            if now < r.fill_deadline <= now + timedelta(hours=config.REMIND_BEFORE_DEADLINE_HOURS):
                if self._once(r.page_id, "pre_deadline"):
                    mentions = " ".join(f"<@{a.discord_user_id}>" for a in unfilled)
                    if channel:
                        await channel.send(
                            f"⏰ 提醒 {mentions}：排課「**{r.name}**」的可用時間 "
                            f"**{r.fill_deadline.strftime('%m/%d %H:%M')}** 截止，還沒填的請趕快填寫！"
                        )
                    for a in unfilled:
                        try:
                            user = await self.bot.fetch_user(int(a.discord_user_id))
                            await user.send(
                                f"⏰ 提醒：排課「{r.name}」的可用時間即將截止"
                                f"（{r.fill_deadline.strftime('%m/%d %H:%M')}），請盡快到排課頻道填寫。"
                            )
                        except Exception:
                            pass
                    await audit.log_event(
                        event_name=f"截止前催收｜{r.name}",
                        action_type="提醒", operator="System", source="System",
                        target_db="Schedule Requests", target_url=r.url,
                        after=f"未填：{names}",
                    )

            # 已過截止仍有人未填：通知管理者
            elif now > r.fill_deadline:
                if self._once(r.page_id, "post_deadline"):
                    if admin_channel:
                        await admin_channel.send(
                            f"🔴 排課「**{r.name}**」填寫已截止，仍有未填藝人：{names}\n"
                            f"可用 `/排課比對` 先看已填的部分，或直接聯絡藝人補填。"
                        )
                    await audit.log_event(
                        event_name=f"截止後未填通知｜{r.name}",
                        action_type="提醒", operator="System", source="System",
                        target_db="Schedule Requests", target_url=r.url,
                        after=f"逾期未填：{names}",
                    )

    async def _check_sla(self):
        now = datetime.now(config.TZ)
        requests = await repositories.get_requests_by_status(repositories.OPEN_REQUEST_STATUSES)
        admin_channel = self.bot.get_channel(config.ADMIN_NOTIFY_CHANNEL_ID) \
            or self.bot.get_channel(config.SCHEDULE_CHANNEL_ID)
        for r in requests:
            if not r.created_time or r.sla_breached:
                continue
            age_hours = (now - r.created_time).total_seconds() / 3600
            if age_hours > config.SLA_HOURS:
                try:
                    await repositories.mark_request_sla_breached(r.page_id)
                except Exception:
                    logger.exception("勾選 SLA 失敗")
                    continue
                hard = "（已超過 3 天硬上限，建議重新蒐集時間）" if age_hours > config.SLA_HARD_LIMIT_HOURS else ""
                if admin_channel:
                    await admin_channel.send(
                        f"🚨 排課「**{r.name}**」已超過 2 天 SLA（{age_hours:.0f} 小時），"
                        f"目前狀態：{r.status}{hard}"
                    )
                await audit.log_event(
                    event_name=f"SLA 升級通知｜{r.name}",
                    action_type="提醒", operator="System", source="System",
                    target_db="Schedule Requests", target_url=r.url,
                    after=f"已 {age_hours:.0f} 小時未完成發布通告",
                )

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
        admin_channel = self.bot.get_channel(config.ADMIN_NOTIFY_CHANNEL_ID) \
            or self.bot.get_channel(config.SCHEDULE_CHANNEL_ID)

        for t, stages in due:
            for stage in stages:
                self._once(f"task:{t.page_id}", stage)  # 一次標記全部，補跑不重複發
            is_overdue = repositories.OVERDUE_STAGE in stages
            deadline_text = tasks_artist._deadline_text(t, now)
            artists = [artist_map[pid] for pid in t.artist_page_ids if pid in artist_map]

            # DM 藝人（僅 Discord 可見的任務；內部行政任務不打擾藝人）
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
