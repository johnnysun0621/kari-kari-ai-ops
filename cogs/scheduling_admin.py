"""管理者端：/排課建立 /排課比對 /排課確認 /排課狀態（限管理身分組）。"""
import logging
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs.scheduling_artist import make_announcement_view
from services import audit, overlap, repositories, timeparse

logger = logging.getLogger(__name__)


def is_admin():
    def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
        return any(r.id == config.ADMIN_ROLE_ID for r in getattr(interaction.user, "roles", []))
    return app_commands.check(predicate)


COURSE_TYPE_CHOICES = [app_commands.Choice(name=t, value=t) for t in config.COURSE_TYPES]


async def _open_request_choices(current: str) -> list[app_commands.Choice[str]]:
    """/排課比對、/排課確認、autocomplete 共用：列出進行中的排課需求。"""
    try:
        requests = await repositories.get_requests_by_status(repositories.OPEN_REQUEST_STATUSES)
    except Exception:
        logger.exception("autocomplete 讀取排課需求失敗")
        return []
    choices = []
    for r in requests:
        label = f"[{r.status}] {r.name}"
        if current and current not in label:
            continue
        choices.append(app_commands.Choice(name=label[:100], value=r.page_id))
    return choices[:25]


class ArtistSelectView(discord.ui.View):
    """/排課建立 的第二步：勾選參與藝人後建立需求並發公告。"""

    def __init__(
        self, artists: list[repositories.Artist],
        name: str, course_type: str, duration: int,
        deadline: datetime, teacher: str, month_week: str, note: str,
    ):
        super().__init__(timeout=600)
        self.artists = {a.page_id: a for a in artists}
        self.params = dict(
            name=name, course_type=course_type, duration=duration,
            deadline=deadline, teacher=teacher, month_week=month_week, note=note,
        )
        options = [
            discord.SelectOption(label=a.name[:100], value=a.page_id)
            for a in artists[:25]
        ]
        self.select = discord.ui.Select(
            placeholder="選擇參與藝人（可多選）…",
            options=options, min_values=1, max_values=len(options),
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        p = self.params
        chosen = [self.artists[pid] for pid in self.select.values]
        try:
            request = await repositories.create_schedule_request(
                name=p["name"], course_type=p["course_type"],
                duration_minutes=p["duration"],
                artist_page_ids=[a.page_id for a in chosen],
                fill_deadline=p["deadline"], teacher=p["teacher"],
                month_week=p["month_week"], note=p["note"],
            )
        except Exception:
            logger.exception("建立排課需求失敗")
            await interaction.followup.send("❌ 建立 Notion 排課需求失敗，請確認 integration 權限。", ephemeral=True)
            return

        channel = interaction.client.get_channel(config.SCHEDULE_CHANNEL_ID)
        mentions = " ".join(f"<@{a.discord_user_id}>" for a in chosen)
        deadline_str = p["deadline"].strftime("%m/%d %H:%M")
        embed = discord.Embed(
            title=f"📅 排課需求｜{p['name']}",
            description=(
                f"**課程類型**：{p['course_type']}\n"
                f"**時長**：{p['duration']} 分鐘\n"
                f"**老師**：{p['teacher']}\n"
                f"**參與藝人**：{ '、'.join(a.name for a in chosen) }\n\n"
                f"請點下方按鈕填寫你的可用時間（可填多筆）。\n"
                f"⏰ **填寫截止：{deadline_str}**"
            ),
            color=discord.Color.green(),
        )
        announce_msg = None
        if channel:
            announce_msg = await channel.send(
                content=mentions, embed=embed,
                view=make_announcement_view(request.page_id),
            )
        # DM 每位藝人
        dm_failed: list[str] = []
        for a in chosen:
            try:
                user = await interaction.client.fetch_user(int(a.discord_user_id))
                await user.send(
                    f"👋 {a.name}，你有新的排課需要填寫可用時間：**{p['name']}**（{p['course_type']}）\n"
                    f"⏰ 截止：{deadline_str}\n"
                    + (f"請到伺服器點擊公告按鈕填寫：{announce_msg.jump_url}" if announce_msg else "請到排課頻道的公告填寫。")
                )
            except Exception:
                dm_failed.append(a.name)

        await audit.log_event(
            event_name=f"建立排課需求｜{p['name']}",
            action_type="建立", operator=str(interaction.user),
            discord_user_id=str(interaction.user.id),
            target_db="Schedule Requests", target_url=request.url,
            after=f"{p['course_type']}｜{p['duration']}分｜{len(chosen)}位藝人｜截止 {deadline_str}",
        )
        note = f"\n⚠️ DM 未送達：{ '、'.join(dm_failed) }（可能未開私訊）" if dm_failed else ""
        where = "已在排課頻道發布公告" if announce_msg else "⚠️ 找不到排課頻道，公告未發出（請檢查 SCHEDULE_CHANNEL_ID）"
        await interaction.followup.send(
            f"✅ 排課需求已建立：**{p['name']}**（{len(chosen)} 位藝人）\n{where}{note}", ephemeral=True
        )
        self.stop()


class CandidateSelectView(discord.ui.View):
    """/排課比對 結果：勾選最多 5 個候選時段回寫 Notion。"""

    def __init__(self, request: repositories.ScheduleRequest, windows: list[overlap.Interval]):
        super().__init__(timeout=600)
        self.request = request
        self.windows = windows
        options = [
            discord.SelectOption(
                label=timeparse.humanize(s, e)[:100],
                value=str(i),
                description=f"可排 {int((e - s).total_seconds() // 60)} 分鐘",
            )
            for i, (s, e) in enumerate(windows[:25])
        ]
        self.select = discord.ui.Select(
            placeholder="勾選要給老師確認的候選時段（最多 5 個）…",
            options=options, min_values=1, max_values=min(5, len(options)),
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        chosen = [self.windows[int(v)] for v in sorted(self.select.values, key=int)]
        text = "\n".join(timeparse.format_candidate(s, e) for s, e in chosen)
        try:
            await repositories.set_request_candidates(self.request.page_id, text)
        except Exception:
            logger.exception("回寫候選時段失敗")
            await interaction.followup.send("❌ 回寫 Notion 失敗，請稍後再試。", ephemeral=True)
            return
        await audit.log_event(
            event_name=f"選定候選時段｜{self.request.name}",
            action_type="更新", operator=str(interaction.user),
            discord_user_id=str(interaction.user.id),
            target_db="Schedule Requests", target_url=self.request.url,
            before=self.request.status or "", after=f"等待老師確認｜{len(chosen)} 個候選",
        )
        lines = "\n".join(f"・{timeparse.humanize(s, e)}" for s, e in chosen)
        await interaction.followup.send(
            f"✅ 已選定 {len(chosen)} 個候選時段，狀態改為「等待老師確認」：\n{lines}\n\n"
            f"請用你的管道向老師（{self.request.teacher or '未填'}）確認後，執行 `/排課確認`。",
            ephemeral=True,
        )
        self.stop()


class FinalizeModal(discord.ui.Modal, title="確認最終上課時間"):
    """老師同意後，填最終開始時間與地點，建立正式課程並發通告。"""

    def __init__(self, request: repositories.ScheduleRequest, window: overlap.Interval):
        super().__init__()
        self.request = request
        self.window = window
        start, end = window
        self.start_input = discord.ui.TextInput(
            label=f"開始時間（{timeparse.humanize(start, end)} 內）",
            default=start.strftime("%H:%M"), max_length=8,
        )
        self.location_input = discord.ui.TextInput(
            label="地點／線上連結（選填）", required=False, max_length=100,
        )
        self.add_item(self.start_input)
        self.add_item(self.location_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        win_start, win_end = self.window
        try:
            h, m = timeparse.parse_time(self.start_input.value)
        except timeparse.ParseError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        start = win_start.replace(hour=h, minute=m)
        if start < win_start:
            start += timedelta(days=1)  # 跨日窗，例如 22:00~01:00 填 00:30
        end = start + timedelta(minutes=self.request.duration_minutes)
        if not (win_start <= start and end <= win_end):
            await interaction.followup.send(
                f"❌ {start.strftime('%H:%M')} 開始上 {self.request.duration_minutes} 分鐘會超出候選窗 "
                f"{timeparse.humanize(win_start, win_end)}，請重新執行 /排課確認。",
                ephemeral=True,
            )
            return

        req = self.request
        session_name = f"{req.course_type or '課程'}｜{start.strftime('%m/%d')}｜{req.name}"
        try:
            session_page = await repositories.create_course_session(
                name=session_name, course_type=req.course_type or "歌唱課",
                artist_page_ids=req.artist_page_ids, teacher=req.teacher,
                start=start, end=end, request_page_id=req.page_id,
                location=self.location_input.value or "",
            )
            await repositories.set_request_confirmed(
                req.page_id, start, end, session_page["id"],
                notice_url=session_page.get("url", ""),
            )
        except Exception:
            logger.exception("建立正式課程失敗")
            await interaction.followup.send("❌ 建立 Notion 正式課程失敗，請稍後再試。", ephemeral=True)
            return

        # 發正式通告：頻道 + DM
        artists = await repositories.get_artists_by_ids(req.artist_page_ids)
        mentions = " ".join(f"<@{a.discord_user_id}>" for a in artists if a.discord_user_id)
        embed = discord.Embed(
            title=f"📢 正式通告｜{req.name}",
            description=(
                f"**課程**：{req.course_type}\n"
                f"**時間**：{timeparse.humanize(start, end)}\n"
                f"**老師**：{req.teacher or '待定'}\n"
                f"**地點**：{self.location_input.value or '待通知'}\n"
                f"**參與**：{ '、'.join(a.name for a in artists) }"
            ),
            color=discord.Color.gold(),
        )
        channel = interaction.client.get_channel(config.SCHEDULE_CHANNEL_ID)
        notify_parts = []
        if channel:
            msg = await channel.send(content=mentions, embed=embed)
            notify_parts.append(f"頻道 {msg.jump_url}")
        dm_ok = 0
        for a in artists:
            if not a.discord_user_id:
                continue
            try:
                user = await interaction.client.fetch_user(int(a.discord_user_id))
                await user.send(embed=embed)
                dm_ok += 1
            except Exception:
                pass
        notify_parts.append(f"DM {dm_ok}/{len(artists)} 位")
        notify_record = f"{datetime.now(config.TZ).strftime('%Y-%m-%d %H:%M')}｜" + "、".join(notify_parts)
        try:
            await repositories.mark_session_notified(session_page["id"], notify_record)
        except Exception:
            logger.exception("回寫通知紀錄失敗")

        await audit.log_event(
            event_name=f"發布正式通告｜{session_name}",
            action_type="建立", operator=str(interaction.user),
            discord_user_id=str(interaction.user.id),
            target_db="Course Sessions", target_url=session_page.get("url", ""),
            after=f"{timeparse.humanize(start, end)}｜{notify_record}",
        )
        await interaction.followup.send(
            f"✅ 正式課程已建立並發布通告！\n**{session_name}**\n{timeparse.humanize(start, end)}\n{ '、'.join(notify_parts) }",
            ephemeral=True,
        )


class ConfirmCandidateView(discord.ui.View):
    """/排課確認 第二步：從候選中選一個，跳出最終確認 Modal。"""

    def __init__(self, request: repositories.ScheduleRequest, candidates: list[overlap.Interval]):
        super().__init__(timeout=600)
        self.request = request
        self.candidates = candidates
        options = [
            discord.SelectOption(label=timeparse.humanize(s, e)[:100], value=str(i))
            for i, (s, e) in enumerate(candidates[:25])
        ]
        self.select = discord.ui.Select(placeholder="選擇老師同意的時段…", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        window = self.candidates[int(self.select.values[0])]
        await interaction.response.send_modal(FinalizeModal(self.request, window))
        self.stop()


class SchedulingAdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------- /排課建立 ----------
    @app_commands.command(name="排課建立", description="建立排課需求並通知藝人填寫可用時間")
    @app_commands.describe(
        名稱="排課名稱，例：7月歌唱課第2週",
        課程類型="課程類型",
        時長分鐘="課程時長（分鐘）",
        老師="老師或確認對象名稱",
        截止小時="幾小時內要填完可用時間（預設 24）",
        月份週次="例：2026-07 W3（選填）",
        備註="其他說明（選填）",
    )
    @app_commands.choices(課程類型=COURSE_TYPE_CHOICES)
    @is_admin()
    async def create_schedule(
        self, interaction: discord.Interaction,
        名稱: str, 課程類型: app_commands.Choice[str],
        時長分鐘: app_commands.Range[int, 15, 600],
        老師: str,
        截止小時: app_commands.Range[int, 1, 168] = config.DEFAULT_FILL_DEADLINE_HOURS,
        月份週次: str = "", 備註: str = "",
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            artists = await repositories.get_active_artists()
        except Exception:
            logger.exception("讀取藝人清單失敗")
            await interaction.followup.send(
                "❌ 讀不到 Notion 藝人資料，請確認 NOTION_TOKEN 與資料庫已 share 給 integration。",
                ephemeral=True,
            )
            return
        if not artists:
            await interaction.followup.send(
                "❌ P0 Artists 裡沒有「Active 且填了 Discord User ID」的藝人，請先到 Notion 補資料。",
                ephemeral=True,
            )
            return
        deadline = datetime.now(config.TZ) + timedelta(hours=截止小時)
        await interaction.followup.send(
            f"排課「**{名稱}**」（{課程類型.value}｜{時長分鐘}分｜截止 {deadline.strftime('%m/%d %H:%M')}）\n請選擇參與藝人：",
            view=ArtistSelectView(
                artists, 名稱, 課程類型.value, 時長分鐘, deadline, 老師, 月份週次, 備註
            ),
            ephemeral=True,
        )

    # ---------- /排課比對 ----------
    @app_commands.command(name="排課比對", description="比對已填寫的可用時間，找出共同時段")
    @app_commands.describe(排課="選擇要比對的排課需求")
    @is_admin()
    async def match_schedule(self, interaction: discord.Interaction, 排課: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        request = await repositories.get_request(排課)
        if request is None:
            await interaction.followup.send("❌ 找不到這筆排課需求。", ephemeral=True)
            return
        artists = await repositories.get_artists_by_ids(request.artist_page_ids)
        slots = await repositories.get_slots_for_request(request.page_id)

        participants: dict[str, list[overlap.Interval]] = {a.page_id: [] for a in artists}
        for s in slots:
            for pid in s.artist_page_ids:
                if pid in participants and s.start and s.end:
                    participants[pid].append((s.start, s.end))
        name_of = {a.page_id: a.name for a in artists}
        missing = [name_of[pid] for pid, ivs in participants.items() if not ivs]

        windows = overlap.find_common_windows(participants, request.duration_minutes)
        now = datetime.now(config.TZ)
        windows = [(s, e) for s, e in windows if e > now]

        if request.status == "等待藝人填寫":
            await repositories.update_request_status(request.page_id, "比對中")
        await audit.log_event(
            event_name=f"比對共同時段｜{request.name}",
            action_type="查詢", operator=str(interaction.user),
            discord_user_id=str(interaction.user.id),
            target_db="Schedule Requests", target_url=request.url,
            after=f"{len(windows)} 個共同時段｜未填：{ '、'.join(missing) if missing else '無' }",
        )

        header = (
            f"**{request.name}**（{request.course_type}｜需 {request.duration_minutes} 分鐘）\n"
            f"已填 {len(artists) - len(missing)}/{len(artists)} 位"
            + (f"｜⚠️ 未填：{ '、'.join(missing) }" if missing else "")
        )
        if not windows:
            hint = "有人還沒填，先催收或等填完再比對。" if missing else "全員都填了但沒有符合時長的共同時段，可能需要請藝人補填或放寬。"
            await interaction.followup.send(f"{header}\n\n❌ 目前沒有可用的共同時段。{hint}", ephemeral=True)
            return
        listing = "\n".join(
            f"`{i+1}.` {timeparse.humanize(s, e)}（可排 {int((e - s).total_seconds() // 60)} 分）"
            for i, (s, e) in enumerate(windows[:25])
        )
        await interaction.followup.send(
            f"{header}\n\n🎯 共同可用時段：\n{listing}\n\n請勾選要向老師確認的候選：",
            view=CandidateSelectView(request, windows),
            ephemeral=True,
        )

    @match_schedule.autocomplete("排課")
    async def match_autocomplete(self, interaction: discord.Interaction, current: str):
        return await _open_request_choices(current)

    # ---------- /排課確認 ----------
    @app_commands.command(name="排課確認", description="老師同意後，確認最終時段並發布正式通告")
    @app_commands.describe(排課="選擇要確認的排課需求")
    @is_admin()
    async def confirm_schedule(self, interaction: discord.Interaction, 排課: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        request = await repositories.get_request(排課)
        if request is None:
            await interaction.followup.send("❌ 找不到這筆排課需求。", ephemeral=True)
            return
        candidates = timeparse.parse_candidates_text(request.candidates_text)
        if not candidates:
            await interaction.followup.send(
                "❌ 這筆需求還沒有候選時段，請先執行 `/排課比對` 勾選候選。", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"**{request.name}**｜老師：{request.teacher or '未填'}\n選擇老師同意的時段（會接著填最終開始時間）：",
            view=ConfirmCandidateView(request, candidates),
            ephemeral=True,
        )

    @confirm_schedule.autocomplete("排課")
    async def confirm_autocomplete(self, interaction: discord.Interaction, current: str):
        return await _open_request_choices(current)

    # ---------- /排課狀態 ----------
    @app_commands.command(name="排課狀態", description="查看所有進行中排課需求的狀態與 SLA")
    @is_admin()
    async def schedule_status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        requests = await repositories.get_requests_by_status(repositories.OPEN_REQUEST_STATUSES)
        if not requests:
            await interaction.followup.send("目前沒有進行中的排課需求。", ephemeral=True)
            return
        now = datetime.now(config.TZ)
        lines = []
        for r in sorted(requests, key=lambda x: x.created_time or now):
            filled = 0
            try:
                slots = await repositories.get_slots_for_request(r.page_id)
                filled_ids = {pid for s in slots for pid in s.artist_page_ids}
                filled = len(filled_ids & set(r.artist_page_ids))
            except Exception:
                pass
            age_h = (now - r.created_time).total_seconds() / 3600 if r.created_time else 0
            sla = "🔴 超過 2 天 SLA" if (r.sla_breached or age_h > config.SLA_HOURS) else f"🟢 {age_h:.0f}h"
            ddl = r.fill_deadline.strftime("%m/%d %H:%M") if r.fill_deadline else "—"
            lines.append(
                f"**{r.name}**｜{r.status}\n"
                f"　已填 {filled}/{len(r.artist_page_ids)}｜截止 {ddl}｜{sla}"
            )
        await interaction.followup.send("📋 進行中排課需求：\n\n" + "\n".join(lines), ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CheckFailure):
            msg = "❌ 這個指令僅限管理者使用。"
        else:
            logger.exception("指令執行錯誤", exc_info=error)
            msg = "❌ 指令執行出錯，請稍後再試（詳見 bot log）。"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SchedulingAdminCog(bot))
