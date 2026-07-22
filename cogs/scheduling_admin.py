"""管理者端：/排課建立 /排課比對 /排課確認 /排課狀態（限管理身分組）。"""
import logging
import time
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs import channel_guard, ui_text
from cogs.scheduling_artist import make_announcement_view, make_pool_availability_view
from services import arrangement, audit, overlap, repositories, scheduling_flow, teacher_confirm, time_collection, timeparse
from services.permissions import is_admin

logger = logging.getLogger(__name__)

# autocomplete 每個鍵擊都會觸發；用短 TTL 快取進行中需求，避免每個字都打 Notion
# （autocomplete 有 3 秒硬上限且不能 defer，Notion 一慢就會「互動失敗」）
# 快取以租戶的 Schedule Requests 資料源為 key，多租戶時互不汙染。
_OPEN_REQUESTS_TTL = 20.0
_open_requests_cache: dict[str, tuple[float, list[repositories.ScheduleRequest]]] = {}


def _invalidate_open_requests_cache() -> None:
    _open_requests_cache.clear()


async def _get_open_requests_cached() -> list[repositories.ScheduleRequest]:
    key = config.get_tenant().ds_schedule_requests
    now = time.monotonic()
    cached = _open_requests_cache.get(key)
    if cached is not None and now - cached[0] < _OPEN_REQUESTS_TTL:
        return cached[1]
    requests = await repositories.get_requests_by_status(repositories.OPEN_REQUEST_STATUSES)
    _open_requests_cache[key] = (now, requests)
    return requests


COURSE_TYPE_CHOICES = [app_commands.Choice(name=t, value=t) for t in config.COURSE_TYPES]
AVAILABILITY_CLEANUP_PREVIEW_LIMIT = 20
MONTHLY_POOL_RESULT_LIMIT = 10
MONTHLY_POOL_START_TIME_LIMIT = 14


async def _open_request_choices(current: str) -> list[app_commands.Choice[str]]:
    """/排課比對、/排課確認、autocomplete 共用：列出進行中的排課需求（短 TTL 快取）。"""
    try:
        requests = await _get_open_requests_cached()
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


def _admin_slot_line(slot: repositories.AvailabilitySlot) -> str:
    if slot.start and slot.end:
        time_text = time_collection.format_slot_summary(slot.start, slot.end, slot.note)
    else:
        time_text = slot.name or "未命名時段"
    return f"{slot.name or '未命名時段'}｜{time_text}"


def _pool_window_text(start: datetime, end: datetime) -> str:
    if start.date() == end.date():
        return f"{start.month}/{start.day} {start:%H:%M}-{end:%H:%M}"
    return f"{start.month}/{start.day} {start:%H:%M}-{end.month}/{end.day} {end:%H:%M}"


def _format_start_times(starts: list[datetime]) -> str:
    if not starts:
        return "無"
    labels = [start.strftime("%H:%M") for start in starts]
    if len(labels) <= MONTHLY_POOL_START_TIME_LIMIT:
        return "、".join(labels)
    shown = "、".join(labels[:MONTHLY_POOL_START_TIME_LIMIT])
    return f"{shown}、...（共 {len(labels)} 個）"


def _monthly_pool_result_text(
    month: str,
    duration_minutes: int,
    artists: list[repositories.Artist],
    result: repositories.MonthlyPoolMatchResult,
) -> str:
    name_of = {artist.page_id: artist.name for artist in artists}
    missing = [name_of[pid] for pid in result.missing_artist_page_ids if pid in name_of]
    filled_count = len(artists) - len(missing)
    lines = [
        f"**{month} 時間池比對**｜需要 {repositories.format_duration_hours(duration_minutes)}",
        f"比對藝人：{ '、'.join(artist.name for artist in artists) }",
        f"已填月度時段：{filled_count}/{len(artists)} 位",
    ]
    if missing:
        lines.append(f"⚠️ 未填：{ '、'.join(missing) }")

    if not result.windows:
        lines.append("")
        lines.append("目前沒有符合條件的共同空檔。")
        if missing:
            lines.append("可以先請未填藝人補填，或改選已填的人先安排。")
        else:
            lines.append("可以改選較少藝人、縮短需要時長，或請藝人補填更多時段。")
        return ui_text.trim_message("\n".join(lines))

    lines.append("")
    lines.append("共同空檔：")
    for index, (start, end) in enumerate(result.windows[:MONTHLY_POOL_RESULT_LIMIT], start=1):
        window_minutes = int((end - start).total_seconds() // 60)
        starts = repositories.candidate_start_times(start, end, duration_minutes)
        lines.append(
            f"`{index}.` {_pool_window_text(start, end)}｜可排 {repositories.format_duration_hours(window_minutes)}\n"
            f"　可開始：{_format_start_times(starts)}"
        )
    if len(result.windows) > MONTHLY_POOL_RESULT_LIMIT:
        lines.append(f"另有 {len(result.windows) - MONTHLY_POOL_RESULT_LIMIT} 個共同空檔未顯示。")
    lines.append("")
    lines.append("這裡只列候選，不會建立 Notion Project / Task。正式安排請由營運判斷後建立。")
    return ui_text.trim_message("\n".join(lines))


def _cleanup_preview_text(
    future_slots: list[repositories.AvailabilitySlot],
    compaction: repositories.AvailabilitySlotCompaction,
) -> str:
    slot_by_id = {slot.page_id: slot for slot in future_slots}
    lines = [
        "🧹 **時間池整理預覽**",
        f"掃描未來時段：{len(future_slots)} 筆",
        f"會合併更新：{len(compaction.updates)} 筆",
        f"建議封存重複 / 冗餘：{len(compaction.redundant)} 筆",
    ]
    if compaction.updates:
        lines.append("\n**會合併更新**")
        for update in compaction.updates[:AVAILABILITY_CLEANUP_PREVIEW_LIMIT]:
            slot = slot_by_id.get(update.page_id)
            before = _admin_slot_line(slot) if slot else update.page_id
            after = time_collection.format_slot_summary(
                update.start, update.end, slot.note if slot else ""
            )
            lines.append(f"・{before}\n　→ {after}")
        if len(compaction.updates) > AVAILABILITY_CLEANUP_PREVIEW_LIMIT:
            lines.append(f"・另有 {len(compaction.updates) - AVAILABILITY_CLEANUP_PREVIEW_LIMIT} 筆合併更新")
    if compaction.redundant:
        lines.append("\n**建議封存**")
        for slot in compaction.redundant[:AVAILABILITY_CLEANUP_PREVIEW_LIMIT]:
            lines.append(f"・{_admin_slot_line(slot)}")
        if len(compaction.redundant) > AVAILABILITY_CLEANUP_PREVIEW_LIMIT:
            lines.append(f"・另有 {len(compaction.redundant) - AVAILABILITY_CLEANUP_PREVIEW_LIMIT} 筆建議封存")
    if not compaction.updates and not compaction.redundant:
        lines.append("\n目前沒有需要整理的重複或重疊資料。")
    else:
        lines.append("\n確認後才會更新 / 封存 Notion 資料。")
    return ui_text.trim_message("\n".join(lines))


class AvailabilityCleanupPreviewView(discord.ui.View):
    def __init__(
        self,
        slots: list[repositories.AvailabilitySlot],
        compaction: repositories.AvailabilitySlotCompaction,
        operator: discord.abc.User,
    ):
        super().__init__(timeout=600)
        self.slots = slots
        self.compaction = compaction
        self.operator_id = operator.id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.operator_id:
            return True
        await interaction.response.send_message("❌ 這個整理預覽只能由原本執行指令的人操作。", ephemeral=True)
        return False

    @discord.ui.button(label="套用全部整理", style=discord.ButtonStyle.danger, emoji="✅")
    async def apply_cleanup(self, interaction: discord.Interaction, _: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="⏳ 正在套用時間池整理，請稍候...",
            view=self,
        )
        try:
            # 套用前重拉最新資料：預覽到按下按鈕之間可能有人新增/刪除時段，
            # 用舊快照套用會封存到已變動的資料（TOCTOU）。
            all_slots = await repositories.get_all_availability_slots()
            now = datetime.now(config.TZ)
            current_slots = [slot for slot in all_slots if slot.end and slot.end > now]
            result = await repositories.apply_availability_slot_compaction(current_slots)
        except Exception:
            logger.exception("套用時間池整理失敗")
            await interaction.followup.send("❌ 套用整理失敗，請稍後再試或查看 bot log。", ephemeral=True)
            return
        changed_note = ""
        if (len(result.updates), len(result.redundant)) != (
            len(self.compaction.updates), len(self.compaction.redundant)
        ):
            changed_note = "\n⚠️ 資料在預覽後有變動，已按最新狀態整理（筆數與預覽不同）。"
        await audit.log_event(
            event_name="營運套用時間池整理",
            action_type="更新",
            operator=str(interaction.user),
            discord_user_id=str(interaction.user.id),
            target_db="Availability Slots",
            before=f"{len(current_slots)} 筆未來時段",
            after=f"更新 {len(result.updates)} 筆合併時段｜封存 {len(result.redundant)} 筆冗餘時段",
        )
        await interaction.followup.send(
            "✅ 已套用時間池整理。\n"
            f"合併更新：{len(result.updates)} 筆\n"
            f"封存重複 / 冗餘：{len(result.redundant)} 筆"
            + changed_note,
            ephemeral=True,
        )
        self.stop()

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel_cleanup(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="已取消時間池整理，Notion 未變更。", view=None)
        self.stop()


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

        channel = interaction.client.get_channel(channel_guard.announce_channel_id())
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
            announce_msg = await channel_guard.try_send(
                channel,
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
                    + (f"請到伺服器點擊公告按鈕填寫：{announce_msg.jump_url}" if announce_msg else "請到公告頻道的公告填寫。")
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
        where = (
            "已在公告頻道發布公告"
            if announce_msg else
            "⚠️ 公告未發出：找不到公告頻道，或 bot 在公告頻道沒有「檢視頻道 / 傳送訊息 / 嵌入連結」權限。"
        )
        await interaction.followup.send(
            f"✅ 排課需求已建立：**{p['name']}**（{len(chosen)} 位藝人）\n{where}{note}", ephemeral=True
        )
        self.stop()


async def _teacher_link_note(
    request: repositories.ScheduleRequest,
    interaction: discord.Interaction,
    candidate_count: int,
) -> str:
    """產生老師確認連結說明文字；未啟用連結功能時回人工流程指引。"""
    if not teacher_confirm.can_generate_links():
        return f"請用你的管道向老師（{request.teacher or '未填'}）確認後，執行 `/排課確認`。"
    expires_ts = int(time.time()) + teacher_confirm.TOKEN_TTL_HOURS * 3600
    token = teacher_confirm.sign_token(request.page_id, expires_ts)
    link = teacher_confirm.confirm_url(token)
    await audit.log_event(
        event_name=f"產生老師確認連結｜{request.name}",
        action_type="建立", operator=str(interaction.user),
        discord_user_id=str(interaction.user.id),
        target_db="Schedule Requests", target_url=request.url,
        after=f"{candidate_count} 個候選｜{teacher_confirm.TOKEN_TTL_HOURS}h 有效",
    )
    return (
        f"🔗 **老師確認連結**（{teacher_confirm.TOKEN_TTL_HOURS} 小時有效、確認一次即失效）：\n{link}\n"
        f"直接複製傳給老師（LINE 等）；老師可複選時段，回覆後由控方執行 `/排課確認` 最後定案。"
    )


class MonthlyPoolArtistSelectView(discord.ui.View):
    """/時間池比對 的第二步：手動選擇要比對共同空檔的藝人。"""

    def __init__(
        self,
        artists: list[repositories.Artist],
        month: str,
        duration_minutes: int,
        operator: discord.abc.User,
    ):
        super().__init__(timeout=600)
        self.artists = {artist.page_id: artist for artist in artists}
        self.month = month
        self.duration_minutes = duration_minutes
        self.operator = operator
        self.operator_id = operator.id
        options = [
            discord.SelectOption(
                label=artist.name[:100],
                value=artist.page_id,
                description=(artist.status or "合約狀態未填")[:100],
            )
            for artist in artists[:25]
        ]
        self.select = discord.ui.Select(
            placeholder="選擇要比對的藝人（可多選）…",
            options=options,
            min_values=1,
            max_values=len(options),
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.operator_id:
            return True
        await interaction.response.send_message("❌ 這個比對選單只能由原本執行指令的人操作。", ephemeral=True)
        return False

    async def on_select(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        chosen = [self.artists[page_id] for page_id in self.select.values]
        artist_page_ids = [artist.page_id for artist in chosen]
        try:
            slots = await repositories.get_monthly_pool_slots_for_artists(artist_page_ids)
            busy_by_artist: dict[str, list[repositories.BusyInterval]] = {}
            for artist_page_id in artist_page_ids:
                context = await repositories.get_schedule_impact_context_for_artist(artist_page_id)
                busy_by_artist[artist_page_id] = context.busy_intervals
            result = repositories.match_monthly_pool_windows(
                slots=slots,
                artist_page_ids=artist_page_ids,
                month=self.month,
                duration_minutes=self.duration_minutes,
                busy_by_artist_page_id=busy_by_artist,
            )
        except Exception:
            logger.exception("月度時間池比對失敗")
            await interaction.followup.send("❌ 時間池比對失敗，請稍後再試或查看 bot log。", ephemeral=True)
            return

        await audit.log_event(
            event_name=f"月度時間池比對｜{self.month}",
            action_type="查詢",
            operator=str(interaction.user),
            discord_user_id=str(interaction.user.id),
            target_db="Availability Slots",
            after=(
                f"{len(chosen)} 位藝人｜需 {repositories.format_duration_hours(self.duration_minutes)}｜"
                f"共同空檔 {len(result.windows)} 個｜未填 {len(result.missing_artist_page_ids)} 位"
            ),
        )
        result_text = _monthly_pool_result_text(self.month, self.duration_minutes, chosen, result)
        if result.windows:
            await interaction.followup.send(
                result_text + "\n\n⬇️ 先勾選要安排的候選時段，再選擇安排類型：",
                view=MonthlyPoolCandidateSelectView(
                    self.month, self.duration_minutes, chosen,
                    result.windows[:25], self.operator,
                ),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(result_text, ephemeral=True)
        self.stop()


class MonthlyPoolCandidateSelectView(discord.ui.View):
    """時間池比對結果：先勾選候選，再決定正式安排類型。"""

    def __init__(
        self,
        month: str,
        duration_minutes: int,
        artists: list[repositories.Artist],
        windows: list[overlap.Interval],
        operator: discord.abc.User,
    ):
        super().__init__(timeout=600)
        self.month = month
        self.duration_minutes = duration_minutes
        self.chosen_artists = artists
        self.windows = windows
        self.operator_id = operator.id
        options = [
            discord.SelectOption(
                label=timeparse.humanize(s, e)[:100],
                value=str(i),
                description=f"可排 {int((e - s).total_seconds() // 60)} 分鐘",
            )
            for i, (s, e) in enumerate(windows[:25])
        ]
        self.select = discord.ui.Select(
            placeholder="勾選要送老師參考的候選時段（最多 5 個）…",
            options=options, min_values=1, max_values=min(5, len(options)),
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.operator_id:
            return True
        await interaction.response.send_message("❌ 這個選單只能由原本執行比對的人操作。", ephemeral=True)
        return False

    async def on_select(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        chosen = [self.windows[int(v)] for v in sorted(self.select.values, key=int)]
        lines = "\n".join(f"・{timeparse.humanize(s, e)}" for s, e in chosen)
        await interaction.followup.send(
            f"已選候選時段：\n{lines}\n\n⬇️ 請選擇這次要安排的類型：",
            view=MonthlyPoolArrangementView(
                self.month,
                self.duration_minutes,
                self.chosen_artists,
                chosen,
                interaction.user,
                teacher="",
            ),
            ephemeral=True,
        )
        self.stop()


class MonthlyPoolArrangementView(discord.ui.View):
    """候選時段選定後的控方安排類型分流。"""

    def __init__(
        self,
        month: str,
        duration_minutes: int,
        artists: list[repositories.Artist],
        windows: list[overlap.Interval],
        operator: discord.abc.User,
        teacher: str = "",
    ):
        super().__init__(timeout=600)
        self.month = month
        self.duration_minutes = duration_minutes
        self.chosen_artists = artists
        self.windows = windows
        self.operator_id = operator.id
        self.teacher = teacher
        self.select = discord.ui.Select(
            placeholder="選擇安排類型…",
            options=[
                discord.SelectOption(label="課程", value="課程", description="建立確認單，請老師選時段"),
                discord.SelectOption(label="錄音", value="錄音", description="控方定案後直接發布正式通告"),
                discord.SelectOption(label="通告", value="通告", description="控方定案後直接發布正式通告"),
                discord.SelectOption(label="其他", value="其他", description="先建立待確認 Task，不自動發布"),
            ],
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.operator_id:
            return True
        await interaction.response.send_message("❌ 這個選單只能由原本執行比對的人操作。", ephemeral=True)
        return False

    async def on_select(self, interaction: discord.Interaction):
        arrangement_type = self.select.values[0]
        if arrangement_type == "課程":
            await interaction.response.send_message(
                content="請選擇具體課程類型：",
                view=CourseTypeView(
                    self.month, self.duration_minutes, self.chosen_artists,
                    self.windows, interaction.user,
                ), ephemeral=True,
            )
        else:
            names = "、".join(a.name for a in self.chosen_artists)
            candidates = "\n".join(timeparse.format_candidate(s, e) for s, e in self.windows)
            await interaction.response.send_message(
                content=(
                    f"✅ 已保留 **{arrangement_type}** 的候選時段，未建立 Notion Task／Project，也未發布通告。\n\n"
                    f"參與藝人：{names}\n候選時段：\n{candidates}\n\n"
                    "請控方自行到 Notion 建立正式 Project／Task。"
                ),
                view=None,
            )
        self.stop()


class CourseTypeView(discord.ui.View):
    """課程安排的第二層選擇，沿用租戶既有課程類型設定。"""

    def __init__(self, month, duration_minutes, artists, windows, operator):
        super().__init__(timeout=600)
        self.month = month
        self.duration_minutes = duration_minutes
        self.artists = artists
        self.windows = windows
        self.operator_id = operator.id
        course_types = config.get_tenant().course_types[:25]
        self.select = discord.ui.Select(
            placeholder="選擇課程類型…",
            options=[discord.SelectOption(label=course_type, value=course_type) for course_type in course_types],
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction):
        if interaction.user.id == self.operator_id:
            return True
        await interaction.response.send_message("❌ 這個選單只能由原本執行比對的人操作。", ephemeral=True)
        return False

    async def on_select(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            teachers = await repositories.get_teacher_options()
            projects = await repositories.get_course_training_projects()
        except Exception:
            logger.exception("讀取課程 Project / 老師清單失敗")
            await interaction.followup.send("❌ 讀取課程 Project 或老師清單失敗，請確認 Notion 資料庫已分享給 Bot。", ephemeral=True)
            return
        if not teachers:
            await interaction.followup.send("❌ People 裡沒有身份類型為「老師」的人員。", ephemeral=True)
            return
        if not projects:
            await interaction.followup.send(
                "❌ 找不到進行中的「課程培訓」Project，請先在 Notion 建立 Project。",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            "請選擇要歸屬的課程培訓 Project：",
            view=CourseProjectSelectView(
                self.month, self.duration_minutes, self.artists, self.windows,
                interaction.user, self.select.values[0], teachers, projects,
            ),
            ephemeral=True,
        )
        self.stop()


class CourseProjectSelectView(discord.ui.View):
    """課程安排選擇上層 Project，Task 建立後可由營運集中管理。"""

    def __init__(self, month, duration_minutes, artists, windows, operator, course_type, teachers, projects):
        super().__init__(timeout=600)
        self.month = month
        self.duration_minutes = duration_minutes
        self.artists = artists
        self.windows = windows
        self.operator_id = operator.id
        self.course_type = course_type
        self.teachers = teachers
        self.projects = {project.page_id: project for project in projects}
        self.select = discord.ui.Select(
            placeholder="選擇課程培訓 Project…",
            options=[
                discord.SelectOption(label=project.name[:100], value=project.page_id)
                for project in projects[:25]
            ],
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction):
        if interaction.user.id == self.operator_id:
            return True
        await interaction.response.send_message("❌ 這個選單只能由原本執行比對的人操作。", ephemeral=True)
        return False

    async def on_select(self, interaction):
        project_id = self.select.values[0]
        await interaction.response.send_message(
            "請選擇負責老師：",
            view=TeacherSelectView(
                self.month, self.duration_minutes, self.artists, self.windows,
                interaction.user, self.course_type, self.teachers, project_id,
            ),
            ephemeral=True,
        )
        self.stop()


class TeacherSelectView(discord.ui.View):
    """從 People.身份類型＝老師的清單選擇確認對象。"""

    def __init__(self, month, duration_minutes, artists, windows, operator, course_type, teachers, project_page_id):
        super().__init__(timeout=600)
        self.month = month
        self.duration_minutes = duration_minutes
        self.artists = artists
        self.windows = windows
        self.operator_id = operator.id
        self.course_type = course_type
        self.teachers = teachers
        self.project_page_id = project_page_id
        self.select = discord.ui.Select(
            placeholder="選擇老師／確認對象…",
            options=[
                discord.SelectOption(label=name[:100], value=page_id)
                for page_id, name in teachers[:25]
            ],
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction):
        if interaction.user.id == self.operator_id:
            return True
        await interaction.response.send_message("❌ 這個選單只能由原本執行比對的人操作。", ephemeral=True)
        return False

    async def on_select(self, interaction):
        teacher_page_id = self.select.values[0]
        teacher_name = next(
            name for page_id, name in self.teachers if page_id == teacher_page_id
        )
        await interaction.response.send_modal(
            CourseArrangementModal(
                self.month, self.duration_minutes, self.artists,
                self.windows, self.course_type, teacher_page_id, teacher_name,
                self.project_page_id,
            )
        )
        self.stop()


class CourseArrangementModal(discord.ui.Modal, title="課程安排設定"):
    """課程分流：補老師資料後建立確認單與老師連結。"""

    def __init__(self, month, duration_minutes, artists, windows, course_type, teacher_page_id, teacher_name, project_page_id):
        super().__init__()
        self.month = month
        self.duration_minutes = duration_minutes
        self.artists = artists
        self.windows = windows
        self.course_type = course_type
        self.teacher_page_id = teacher_page_id
        self.teacher_name = teacher_name
        self.project_page_id = project_page_id
        self.name_input = discord.ui.TextInput(label="課程名稱", placeholder="例：八月歌唱課｜Johnny Chenhua", max_length=80)
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        request_name = self.name_input.value.strip()
        if not request_name:
            await interaction.followup.send("❌ 請填寫課程名稱。", ephemeral=True)
            return
        names = "、".join(a.name for a in self.artists)
        candidates_text = "\n".join(timeparse.format_candidate(s, e) for s, e in self.windows)
        description = (
            f"安排來源：/時間池比對\n"
            f"課程類別：{self.course_type}\n"
            f"負責老師：{self.teacher_name}\n"
            f"需要時長：{repositories.format_duration_hours(self.duration_minutes)}\n"
            "候選時段（由營運最後確認）：\n"
            f"{candidates_text}\n\n"
            "此 Task 同時管理課程場次與課後作業；正式時間請由營運直接填寫開始日。"
        )
        try:
            task = await repositories.create_formal_arrangement_task(
                name=request_name,
                task_type="課程與作業",
                artist_page_ids=[a.page_id for a in self.artists],
                description=description,
                course_category=self.course_type,
                course_item_nature="課程場次",
                teacher_page_ids=[self.teacher_page_id],
                project_page_ids=[self.project_page_id],
            )
        except Exception:
            logger.exception("建立課程 Task 失敗")
            await interaction.followup.send("❌ 建立課程 Task 失敗，請確認 Tasks 欄位與 Notion 權限。", ephemeral=True)
            return
        await audit.log_event(
            event_name=f"建立課程 Task｜{request_name}",
            action_type="建立", operator=str(interaction.user),
            discord_user_id=str(interaction.user.id),
            target_db="Tasks", target_url=task.url,
            after=f"課程與作業｜{self.course_type}｜老師 {self.teacher_name}｜{len(self.windows)} 個候選｜{names}",
        )
        lines = "\n".join(f"・{timeparse.humanize(s, e)}" for s, e in self.windows)
        teacher_link = ""
        if teacher_confirm.can_generate_links():
            expires_at = int(time.time()) + teacher_confirm.TOKEN_TTL_HOURS * 3600
            token = teacher_confirm.sign_task_token(
                task.page_id,
                expires_at,
                task_name=request_name,
                course_type=self.course_type,
                duration_text=repositories.format_duration_hours(self.duration_minutes),
                teacher=self.teacher_name,
                windows=[timeparse.humanize(start, end) for start, end in self.windows],
            )
            teacher_link = (
                f"\n\n🔗 老師填寫表單（{teacher_confirm.TOKEN_TTL_HOURS} 小時有效）：\n"
                f"{teacher_confirm.confirm_url(token)}\n"
                "請把這個連結傳給老師；老師可多選，最後由控方在 Task 決定正式時間。"
            )
        await interaction.followup.send(
            f"✅ 已建立課程 Task「**{request_name}**」。\n"
            f"課程類別：{self.course_type}｜老師：{self.teacher_name}\n"
            f"候選時段：\n{lines}\n\n"
            "請到 Notion 的 Task 頁面最後確認正式開始／結束時間；課程作業也在同一筆 Task 管理。"
            f"{teacher_link}",
            ephemeral=True,
        )


class DirectArrangementCandidateView(discord.ui.View):
    """錄音／通告分流：由控方選一個候選窗後輸入精確開始時間。"""

    def __init__(self, arrangement_type, month, duration_minutes, artists, windows, operator):
        super().__init__(timeout=600)
        self.arrangement_type = arrangement_type
        self.month = month
        self.duration_minutes = duration_minutes
        self.artists = artists
        self.windows = windows
        self.operator_id = operator.id
        self.select = discord.ui.Select(
            placeholder="選擇要定案的候選時段…",
            options=[
                discord.SelectOption(label=timeparse.humanize(s, e)[:100], value=str(i))
                for i, (s, e) in enumerate(windows[:25])
            ],
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction):
        if interaction.user.id == self.operator_id:
            return True
        await interaction.response.send_message("❌ 這個選單只能由原本執行比對的人操作。", ephemeral=True)
        return False

    async def on_select(self, interaction):
        window = self.windows[int(self.select.values[0])]
        await interaction.response.send_modal(
            DirectArrangementModal(
                self.arrangement_type,
                self.month,
                self.duration_minutes,
                self.artists,
                window,
            )
        )
        self.stop()


class DirectArrangementModal(discord.ui.Modal, title="發布正式安排"):
    def __init__(self, arrangement_type, month, duration_minutes, artists, window):
        super().__init__()
        self.arrangement_type = arrangement_type
        self.month = month
        self.duration_minutes = duration_minutes
        self.artists = artists
        self.window = window
        self.start_input = discord.ui.TextInput(label="開始時間", default=window[0].strftime("%H:%M"), max_length=8)
        self.location_input = discord.ui.TextInput(label="地點／連結（選填）", required=False, max_length=200)
        self.add_item(self.start_input)
        self.add_item(self.location_input)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        win_start, win_end = self.window
        try:
            h, m = timeparse.parse_time(self.start_input.value)
        except timeparse.ParseError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        start = win_start.replace(hour=h, minute=m)
        if start < win_start:
            start += timedelta(days=1)
        end = start + timedelta(minutes=self.duration_minutes)
        if not (win_start <= start and end <= win_end):
            await interaction.followup.send(
                f"❌ 此時間超出候選窗 {timeparse.humanize(win_start, win_end)}，請重新選候選。",
                ephemeral=True,
            )
            return
        names = "、".join(a.name for a in self.artists)
        task_name = f"{self.month}｜{names}｜{self.arrangement_type}｜{start:%m/%d %H:%M}"
        try:
            task = await repositories.create_formal_arrangement_task(
                name=task_name,
                task_type=arrangement.task_type_for_arrangement(self.arrangement_type) or "行政",
                artist_page_ids=[a.page_id for a in self.artists],
                description=f"安排類型：{self.arrangement_type}\n由 /時間池比對確認並直接發布正式通告。",
                start=start,
                end=end,
            )
            result = await scheduling_flow.publish_direct_arrangement(
                interaction.client,
                arrangement_type=self.arrangement_type,
                task=task,
                artists=self.artists,
                start=start,
                end=end,
                location=self.location_input.value or "",
                operator=str(interaction.user),
                discord_user_id=str(interaction.user.id),
            )
        except Exception:
            logger.exception("直接發布正式安排失敗")
            await interaction.followup.send("❌ 正式安排建立或通告發布失敗，請查看 bot log。", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ {self.arrangement_type} 已建立正式 Task 並發布通告！\n"
            f"**{result.task_name}**\n{result.time_text}\n{result.notify_summary}",
            ephemeral=True,
        )


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
        teacher_note = await _teacher_link_note(self.request, interaction, len(chosen))
        await interaction.followup.send(
            f"✅ 已選定 {len(chosen)} 個候選時段，狀態改為「等待老師確認」：\n{lines}\n\n{teacher_note}",
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
        try:
            result = await scheduling_flow.publish_confirmed_schedule(
                interaction.client, req, start, end,
                location=self.location_input.value or "",
                operator=str(interaction.user),
                discord_user_id=str(interaction.user.id),
            )
        except Exception:
            logger.exception("建立正式課程失敗")
            await interaction.followup.send("❌ 建立 Notion 正式課程失敗，請稍後再試。", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ 正式課程已建立並發布通告！\n**{result.session_name}**\n{result.time_text}\n{result.notify_summary}",
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

    # ---------- /時間收集建立 ----------
    @app_commands.command(name="時間收集建立", description="發起月度可配合時段收集，不綁定單一排課需求")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        月份="要收集的月份，例：2026-08",
        截止天數="幾天內要填完月度可配合時段（預設 7 天）",
        說明="補充給藝人的說明（選填）",
    )
    @is_admin()
    async def create_availability_collection(
        self,
        interaction: discord.Interaction,
        月份: str,
        截止天數: app_commands.Range[int, 1, 30] = 7,
        說明: str = "",
    ):
        if not await channel_guard.ensure_schedule_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        month = time_collection.normalize_month(月份)
        if month is None:
            await interaction.followup.send("❌ 月份格式請用 `2026-08`。", ephemeral=True)
            return
        nature = time_collection.collection_nature(time_collection.DEFAULT_COLLECTION_NATURE)
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
                "❌ Artists 裡沒有可排課藝人，請先到 Notion 補 Discord User ID。",
                ephemeral=True,
            )
            return

        deadline = datetime.now(config.TZ) + timedelta(days=截止天數)
        deadline_str = deadline.strftime("%m/%d %H:%M")
        time_collection.register_active_collection(
            month,
            deadline,
            [artist.page_id for artist in artists],
        )
        mentions = " ".join(f"<@{a.discord_user_id}>" for a in artists)
        description = (
            f"{time_collection.availability_announcement_copy(month)}\n\n"
            f"{time_collection.availability_selection_sop()}\n\n"
            "送出時會自動檢查已安排任務；若有衝突，只排除衝突時段。\n"
            f"⏰ **填寫截止：{deadline_str}**"
        )
        if 說明:
            description += f"\n\n**補充說明**：{說明}"
        embed = discord.Embed(
            title=f"📅 {month} 可配合時間回報",
            description=description,
            color=discord.Color.blue(),
        )
        channel = interaction.client.get_channel(channel_guard.announce_channel_id())
        announce_msg = None
        if channel:
            announce_msg = await channel_guard.try_send(
                channel,
                content=mentions,
                embed=embed,
                view=make_pool_availability_view(month, deadline, nature.value),
            )

        dm_failed: list[str] = []
        for artist in artists:
            try:
                user = await interaction.client.fetch_user(int(artist.discord_user_id))
                await user.send(
                    f"👋 {artist.name}，請回報 **{month} 可配合時間**。\n"
                    f"{time_collection.availability_announcement_copy(month)}\n"
                    f"{time_collection.availability_selection_sop()}\n"
                    "送出時會自動檢查已安排任務；若有衝突，只排除衝突時段。\n"
                    f"⏰ 截止：{deadline_str}\n"
                    + (
                        f"請到伺服器公告填寫：{announce_msg.jump_url}"
                        if announce_msg else
                        "請到公告頻道的時間收集公告填寫。"
                    )
                )
            except Exception:
                dm_failed.append(artist.name)

        await audit.log_event(
            event_name=f"建立月度可配合時段收集｜{month}",
            action_type="建立",
            operator=str(interaction.user),
            discord_user_id=str(interaction.user.id),
            target_db="Availability Slots",
            after=f"{nature.label}｜{len(artists)} 位藝人｜截止 {deadline_str}",
        )
        note = f"\n⚠️ DM 未送達：{ '、'.join(dm_failed) }（可能未開私訊）" if dm_failed else ""
        where = (
            "已在公告頻道發布月度可配合時段收集公告"
            if announce_msg else
            "⚠️ 公告未發出：找不到公告頻道，或 bot 在公告頻道沒有「檢視頻道 / 傳送訊息 / 嵌入連結」權限。"
        )
        await interaction.followup.send(
            f"✅ 已建立 **{month}** 月度可配合時段收集。\n{where}{note}",
            ephemeral=True,
        )

    # ---------- /時間池整理 ----------
    @app_commands.command(name="時間池整理", description="主動整理未來可用時段，合併重疊並清除重複資料")
    @app_commands.default_permissions(administrator=True)
    @is_admin()
    async def cleanup_availability_pool(self, interaction: discord.Interaction):
        if not await channel_guard.ensure_schedule_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            all_slots = await repositories.get_all_availability_slots()
            now = datetime.now(config.TZ)
            future_slots = [
                slot for slot in all_slots
                if slot.end and slot.end > now
            ]
            compaction = repositories.compact_availability_slots(future_slots)
        except Exception:
            logger.exception("整理時間池失敗")
            await interaction.followup.send(
                "❌ 產生時間池整理預覽失敗，請稍後再試或查看 bot log。",
                ephemeral=True,
            )
            return

        content = _cleanup_preview_text(future_slots, compaction)
        if not compaction.updates and not compaction.redundant:
            await interaction.followup.send(content, ephemeral=True)
            return
        await interaction.followup.send(
            content,
            view=AvailabilityCleanupPreviewView(future_slots, compaction, interaction.user),
            ephemeral=True,
        )

    # ---------- /時間池比對 ----------
    @app_commands.command(name="時間池比對", description="比對月度可配合時間池，找多位藝人的共同空檔")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        月份="要比對的月份，例：2026-08",
        時長分鐘="需要連續可安排的時長，例：60、90、240",
    )
    @is_admin()
    async def match_availability_pool(
        self,
        interaction: discord.Interaction,
        月份: str,
        時長分鐘: app_commands.Range[int, 15, 600],
    ):
        if not await channel_guard.ensure_schedule_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        month = time_collection.normalize_month(月份)
        if month is None:
            await interaction.followup.send("❌ 月份格式請用 `2026-08`。", ephemeral=True)
            return
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
                "❌ Artists 裡沒有可比對藝人，請先到 Notion 補 Discord User ID。",
                ephemeral=True,
            )
            return
        limit_note = ""
        if len(artists) > 25:
            limit_note = "\n⚠️ Discord 單次選單最多顯示 25 位，這次先列前 25 位可排課藝人。"
        await interaction.followup.send(
            (
                f"請選擇要比對 **{month}** 月度時間池的藝人。\n"
                f"需要連續時長：{repositories.format_duration_hours(時長分鐘)}"
                f"{limit_note}"
            ),
            view=MonthlyPoolArtistSelectView(
                artists[:25], month, 時長分鐘, interaction.user,
            ),
            ephemeral=True,
        )

    # ---------- /排課建立 ----------
    @app_commands.command(name="排課建立", description="建立排課需求並通知藝人填寫可用時間")
    @app_commands.default_permissions(administrator=True)
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
        if not await channel_guard.ensure_schedule_channel(interaction):
            return
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
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(排課="選擇要比對的排課需求")
    @is_admin()
    async def match_schedule(self, interaction: discord.Interaction, 排課: str):
        if not await channel_guard.ensure_schedule_channel(interaction):
            return
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
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(排課="選擇要確認的排課需求")
    @is_admin()
    async def confirm_schedule(self, interaction: discord.Interaction, 排課: str):
        if not await channel_guard.ensure_schedule_channel(interaction):
            return
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
    @app_commands.default_permissions(administrator=True)
    @is_admin()
    async def schedule_status(self, interaction: discord.Interaction):
        if not await channel_guard.ensure_schedule_channel(interaction):
            return
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


# 7/11 會議決議：排課建立流程取消（改由 /時間池比對 直通老師確認）。
# 舊指令程式與測試保留，僅從 Discord 指令選單移除；要臨時啟用改 True 重啟。
LEGACY_SCHEDULING_COMMANDS_VISIBLE = False
LEGACY_SCHEDULING_COMMAND_NAMES = ("排課建立", "排課比對", "排課確認", "排課狀態")


async def setup(bot: commands.Bot):
    await bot.add_cog(SchedulingAdminCog(bot))
    if not LEGACY_SCHEDULING_COMMANDS_VISIBLE:
        for name in LEGACY_SCHEDULING_COMMAND_NAMES:
            bot.tree.remove_command(name)
