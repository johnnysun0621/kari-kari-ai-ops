"""管理者端：月度時間收集與 Task 版正式安排。"""
import logging
import time
from datetime import date, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs import channel_guard, ui_text
from cogs.scheduling_artist import make_pool_availability_view
from services import audit, formal_notice, overlap, repositories, scheduling_flow, scheduling_sla, teacher_confirm, time_collection, timeparse
from services.permissions import is_admin

logger = logging.getLogger(__name__)

AVAILABILITY_CLEANUP_PREVIEW_LIMIT = 20
MONTHLY_POOL_CANDIDATE_LIMIT = 25


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


def _duration_candidates(
    windows: list[overlap.Interval],
    duration_minutes: int,
    limit: int = MONTHLY_POOL_CANDIDATE_LIMIT,
) -> list[overlap.Interval]:
    """把寬的共同空檔切成實際需求時長的候選時段。"""
    candidates: list[overlap.Interval] = []
    seen: set[tuple[datetime, datetime]] = set()
    for start, end in windows:
        for candidate_start in repositories.candidate_start_times(start, end, duration_minutes):
            candidate_end = candidate_start + timedelta(minutes=duration_minutes)
            key = (candidate_start, candidate_end)
            if key not in seen:
                seen.add(key)
                candidates.append(key)
                if len(candidates) >= limit:
                    return candidates
    return candidates


def _format_candidate_groups(candidates: list[overlap.Interval]) -> list[str]:
    """依日期分組候選時段，讓 Discord 訊息保持短而容易掃讀。"""
    weekday_names = ("一", "二", "三", "四", "五", "六", "日")
    grouped: dict[date, list[str]] = {}
    for start, end in candidates:
        grouped.setdefault(start.date(), []).append(f"{start:%H:%M}–{end:%H:%M}")

    lines: list[str] = []
    for candidate_date, times in grouped.items():
        weekday = weekday_names[candidate_date.weekday()]
        lines.append(f"**{candidate_date.month}/{candidate_date.day}（週{weekday}）**")
        for index in range(0, len(times), 3):
            lines.append("　" + "　".join(times[index:index + 3]))
    return lines


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

    candidates = _duration_candidates(result.windows, duration_minutes)
    lines.append("")
    lines.append(f"符合 {repositories.format_duration_hours(duration_minutes)} 的候選時段：")
    lines.extend(_format_candidate_groups(candidates))
    total_candidates = sum(
        len(repositories.candidate_start_times(start, end, duration_minutes))
        for start, end in result.windows
    )
    if total_candidates > len(candidates):
        lines.append(f"另有 {total_candidates - len(candidates)} 個候選時段未顯示。")
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


class MonthlyPoolArtistSelectView(discord.ui.View):
    """/從時間池建立任務 的第二步：手動選擇要比對共同空檔的藝人。"""

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
            # 先整理被選藝人的未來時段，讓比對使用最新且不重複的時間池。
            slots = await repositories.get_monthly_pool_slots_for_artists(artist_page_ids)
            await repositories.apply_availability_slot_compaction(slots)
            # 套用後重新讀取，取得合併後的最新時間。
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
                    _duration_candidates(result.windows, self.duration_minutes), self.operator,
                ),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(result_text, ephemeral=True)
        self.stop()


class MonthlyPoolCandidateSelectView(discord.ui.View):
    """時間池比對結果：先勾選候選，再直接選 Notion 任務類型。"""

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
        scheduling_sla.mark_pool_matched(self.month, int(time.time()))
        lines = "\n".join(f"・{timeparse.humanize(s, e)}" for s, e in chosen)
        try:
            task_types = await repositories.get_task_type_options()
        except Exception:
            logger.exception("讀取 Tasks 任務類型選項失敗")
            await interaction.followup.send(
                "❌ 讀取 Notion Tasks 的任務類型失敗，請確認 Bot 有讀取 Tasks schema 的權限。",
                ephemeral=True,
            )
            return
        if not task_types:
            await interaction.followup.send(
                "❌ Notion Tasks 的「任務類型」目前沒有可用選項。",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"已選候選時段：\n{lines}\n\n⬇️ 請直接選擇 Notion 的任務類型：",
            view=TaskTypeView(
                self.month, self.duration_minutes, self.chosen_artists,
                chosen, interaction.user, task_types,
            ),
            ephemeral=True,
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
        teacher_windows = repositories.split_windows_by_duration(
            self.windows, self.duration_minutes
        )
        names = "、".join(a.name for a in self.artists)
        candidates_text = "\n".join(timeparse.format_candidate(s, e) for s, e in teacher_windows)
        description = (
            f"安排來源：/從時間池建立任務\n"
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
                teacher_page_ids=[self.teacher_page_id],
                project_page_ids=[self.project_page_id],
                discord_visible=False,
            )
        except Exception:
            logger.exception("建立課程 Task 失敗")
            await interaction.followup.send("❌ 建立課程 Task 失敗，請確認 Tasks 欄位與 Notion 權限。", ephemeral=True)
            return
        teacher_deadline_ts = int(time.time()) + teacher_confirm.TOKEN_TTL_HOURS * 3600
        scheduling_sla.register_task(
            task.page_id,
            self.month,
            created_ts=int(time.time()),
            teacher_deadline_ts=teacher_deadline_ts,
        )
        await audit.log_event(
            event_name=f"建立課程 Task｜{request_name}",
            action_type="建立", operator=str(interaction.user),
            discord_user_id=str(interaction.user.id),
            target_db="Tasks", target_url=task.url,
            after=f"課程與作業｜{self.course_type}｜老師 {self.teacher_name}｜{len(teacher_windows)} 個候選｜{names}",
        )
        lines = "\n".join(f"・{timeparse.humanize(s, e)}" for s, e in teacher_windows)
        teacher_link = ""
        if teacher_confirm.can_generate_links():
            expires_at = teacher_deadline_ts
            token = teacher_confirm.sign_task_token(
                task.page_id,
                expires_at,
                task_name=request_name,
                course_type=self.course_type,
                duration_text=repositories.format_duration_hours(self.duration_minutes),
                teacher=self.teacher_name,
                participants=[artist.name for artist in self.artists],
                windows=[timeparse.humanize(start, end) for start, end in teacher_windows],
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
            "目前不會通知藝人；請到 Notion 的 Task 頁面填正式開始／結束時間，之後使用 `/正式通告發布`。課程作業也在同一筆 Task 管理。"
            f"{teacher_link}",
            ephemeral=True,
        )


class TaskTypeView(discord.ui.View):
    """直接從 Notion Tasks schema 選擇正式任務類型。"""

    def __init__(self, month, duration_minutes, artists, windows, operator, task_types):
        super().__init__(timeout=600)
        self.month = month
        self.duration_minutes = duration_minutes
        self.artists = artists
        self.windows = windows
        self.operator_id = operator.id
        self.select = discord.ui.Select(
            placeholder="選擇 Notion 任務類型…",
            options=[discord.SelectOption(label=task_type[:100], value=task_type) for task_type in task_types[:25]],
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction):
        if interaction.user.id == self.operator_id:
            return True
        await interaction.response.send_message("❌ 這個選單只能由原本執行比對的人操作。", ephemeral=True)
        return False

    async def on_select(self, interaction):
        task_type = self.select.values[0]
        if task_type == "課程與作業":
            await interaction.response.send_message(
                content="請選擇具體課程類型：",
                view=CourseTypeView(
                    self.month, self.duration_minutes, self.artists,
                    self.windows, interaction.user,
                ), ephemeral=True,
            )
            self.stop()
            return
        await interaction.response.send_message(
            content=f"請選擇 **{task_type}** 要定案的候選時段：",
            view=DirectArrangementCandidateView(
                task_type, task_type, self.month, self.duration_minutes,
                self.artists, self.windows, interaction.user,
            ),
            ephemeral=True,
        )
        self.stop()


class DirectArrangementCandidateView(discord.ui.View):
    """非課程分流：由控方選候選窗後建立尚未發布的 Task 草稿。"""

    def __init__(self, arrangement_type, task_type, month, duration_minutes, artists, windows, operator):
        super().__init__(timeout=600)
        self.arrangement_type = arrangement_type
        self.task_type = task_type
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
                self.arrangement_type, self.task_type,
                self.month,
                self.duration_minutes,
                self.artists,
                window,
            )
        )
        self.stop()


class DirectArrangementModal(discord.ui.Modal, title="建立安排 Task 草稿"):
    def __init__(self, arrangement_type, task_type, month, duration_minutes, artists, window):
        super().__init__()
        self.arrangement_type = arrangement_type
        self.task_type = task_type
        self.month = month
        self.duration_minutes = duration_minutes
        self.artists = artists
        self.window = window
        self.name_input = discord.ui.TextInput(label="任務名稱", placeholder="例：八月錄音安排", max_length=80)
        self.start_input = discord.ui.TextInput(label="開始時間", default=window[0].strftime("%H:%M"), max_length=8)
        self.location_input = discord.ui.TextInput(label="地點／連結（選填）", required=False, max_length=200)
        self.add_item(self.name_input)
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
        task_name = self.name_input.value.strip()
        if not task_name:
            await interaction.followup.send("❌ 請填寫任務名稱。", ephemeral=True)
            return
        location = self.location_input.value.strip()
        description = (
            f"安排來源：/從時間池建立任務\n"
            f"安排類型：{self.arrangement_type}\n"
            f"任務類型：{self.task_type}\n"
            f"地點／連結：{location or '待補'}\n"
            "此為控方草稿；補齊內容後使用 /正式通告發布。"
        )
        try:
            task = await repositories.create_formal_arrangement_task(
                name=task_name,
                task_type=self.task_type,
                artist_page_ids=[a.page_id for a in self.artists],
                description=description,
                start=start,
                end=end,
                discord_visible=False,
            )
        except Exception:
            logger.exception("建立安排 Task 草稿失敗")
            await interaction.followup.send("❌ Task 草稿建立失敗，請查看 bot log。", ephemeral=True)
            return
        scheduling_sla.register_task(
            task.page_id, self.month, created_ts=int(time.time())
        )
        await audit.log_event(
            event_name=f"建立安排 Task 草稿｜{task.name}",
            action_type="建立", operator=str(interaction.user),
            discord_user_id=str(interaction.user.id),
            target_db="Tasks", target_url=task.url,
            after=f"{self.arrangement_type}｜{timeparse.humanize(start, end)}｜Discord 顯示=false",
        )
        await interaction.followup.send(
            f"✅ 已建立 **{self.arrangement_type}** Task 草稿，尚未通知藝人。\n"
            f"**{task.name}**｜{timeparse.humanize(start, end)}\n"
            f"任務類型：{self.task_type}\n"
            f"🔗 [開啟 Notion Task，補齊內容]({task.url})\n"
            "請先在 Notion 補齊任務內容，再使用 `/正式通告發布` 預覽並發布。",
            ephemeral=True,
        )


def _formal_notice_preview(
    task: repositories.TaskItem,
    artists: list[repositories.Artist],
    teachers: list[repositories.PersonItem],
) -> str:
    location = repositories.task_description_field(
        task.description, "地點", "地點／連結"
    ) or "⚠️ 未填"
    teacher_names = "、".join(person.name for person in teachers) or "無"
    content = task.description.strip() or "⚠️ 未填任務內容"
    return ui_text.trim_message(
        "📣 **正式通告發布預覽**\n"
        f"任務：**{task.name}**\n"
        f"類型：{task.course_category or task.task_type or '未填'}\n"
        f"時間：{timeparse.humanize(task.start, task.end)}\n"
        f"藝人：{'、'.join(artist.name for artist in artists) or '⚠️ 未填'}\n"
        f"老師：{teacher_names}\n"
        f"地點／連結：{location}\n\n"
        f"任務內容：\n{content}\n\n"
        "確認後會發送公告、藝人 DM；有 Discord ID 的老師也會收到通知。"
    )


class FormalNoticePreviewView(discord.ui.View):
    def __init__(self, task_page_id: str, operator: discord.abc.User):
        super().__init__(timeout=600)
        self.task_page_id = task_page_id
        self.operator_id = operator.id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.operator_id:
            return True
        await interaction.response.send_message("❌ 只有建立預覽的控方可以發布。", ephemeral=True)
        return False

    @discord.ui.button(label="確認發布", style=discord.ButtonStyle.danger, emoji="📣")
    async def publish(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if formal_notice.was_published(self.task_page_id):
            await interaction.followup.send("⚠️ 這筆 Task 已發布過正式通告，不會重複發送。", ephemeral=True)
            return
        task = await repositories.get_task(self.task_page_id)
        if task is None or not repositories.is_publishable_formal_task(task):
            await interaction.followup.send(
                "❌ Task 已變更，現在不符合發布條件。請確認藝人、開始與截止時間後重新執行指令。",
                ephemeral=True,
            )
            return
        try:
            artists = await repositories.get_artists_by_ids(task.artist_page_ids)
            teachers = await repositories.get_people_items_by_ids(task.teacher_page_ids)
            result = await scheduling_flow.publish_task_notice(
                interaction.client,
                task=task,
                artists=artists,
                teachers=teachers,
                operator=str(interaction.user),
                discord_user_id=str(interaction.user.id),
            )
        except ValueError as exc:
            if str(exc) == "task_already_published":
                await interaction.followup.send("⚠️ 這筆 Task 已發布過正式通告。", ephemeral=True)
                return
            logger.exception("正式通告 Task 驗證失敗")
            await interaction.followup.send("❌ Task 不符合正式通告發布條件。", ephemeral=True)
            return
        except Exception:
            logger.exception("正式通告發布失敗")
            await interaction.followup.send(
                "❌ 正式通告未成功送達任何管道，未標記為已發布，請檢查頻道與 DM 權限後重試。",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"✅ 正式通告已發布：**{result.task_name}**\n"
            f"{result.time_text}\n{result.notify_summary}",
            ephemeral=True,
        )
        self.stop()


class FormalNoticeTaskSelectView(discord.ui.View):
    def __init__(self, tasks: list[repositories.TaskItem], operator: discord.abc.User):
        super().__init__(timeout=600)
        self.tasks = {task.page_id: task for task in tasks}
        self.operator_id = operator.id
        self.select = discord.ui.Select(
            placeholder="選擇要發布的正式 Task…",
            options=[
                discord.SelectOption(
                    label=task.name[:100],
                    value=task.page_id,
                    description=timeparse.humanize(task.start, task.end)[:100],
                )
                for task in tasks[:25]
            ],
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.operator_id:
            return True
        await interaction.response.send_message("❌ 這個選單只能由原本執行指令的人操作。", ephemeral=True)
        return False

    async def on_select(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        task = await repositories.get_task(self.select.values[0])
        if task is None or formal_notice.was_published(task.page_id):
            await interaction.followup.send("⚠️ 這筆 Task 已不存在或已發布，請重新執行指令。", ephemeral=True)
            return
        artists = await repositories.get_artists_by_ids(task.artist_page_ids)
        teachers = await repositories.get_people_items_by_ids(task.teacher_page_ids)
        await interaction.followup.send(
            _formal_notice_preview(task, artists, teachers),
            view=FormalNoticePreviewView(task.page_id, interaction.user),
            ephemeral=True,
        )
        self.stop()


class SchedulingAdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------- /時間資訊蒐集 ----------
    @app_commands.command(name="時間資訊蒐集", description="建立回報任務並發起月度可配合時段收集")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        月份="要收集的月份，例：2026-08",
        截止天數="幾天內要填完月度可配合時段（預設 1 天）",
        說明="補充給藝人的說明（選填）",
    )
    @is_admin()
    async def create_availability_collection(
        self,
        interaction: discord.Interaction,
        月份: str,
        截止天數: app_commands.Range[int, 1, 30] = 1,
        說明: str = "",
    ):
        if not await channel_guard.ensure_schedule_channel(interaction):
            return
        # 先立即確認 Discord interaction，後續 Notion、公告與 DM 操作完成後
        # 直接更新同一則回覆，避免長流程超過 Discord 的初始回應期限。
        await interaction.response.send_message(
            "⏳ 正在建立時間資訊蒐集，請稍候…",
            ephemeral=True,
        )
        month = time_collection.normalize_month(月份)
        if month is None:
            await interaction.edit_original_response(content="❌ 月份格式請用 `2026-08`。")
            return
        nature = time_collection.collection_nature(time_collection.DEFAULT_COLLECTION_NATURE)
        try:
            artists = await repositories.get_active_artists()
        except Exception:
            logger.exception("讀取藝人清單失敗")
            await interaction.edit_original_response(
                content="❌ 讀不到 Notion 藝人資料，請確認 NOTION_TOKEN 與資料庫已 share 給 integration。"
            )
            return
        if not artists:
            await interaction.edit_original_response(
                content="❌ Artists 裡沒有可排課藝人，請先到 Notion 補 Discord User ID。"
            )
            return

        deadline = datetime.now(config.TZ) + timedelta(days=截止天數)
        deadline_str = deadline.strftime("%m/%d %H:%M")
        time_collection.register_active_collection(
            month,
            deadline,
            [artist.page_id for artist in artists],
        )
        scheduling_sla.register_collection(
            month,
            created_ts=int(datetime.now(config.TZ).timestamp()),
            fill_deadline_ts=int(deadline.timestamp()),
        )
        mentions = " ".join(f"<@{a.discord_user_id}>" for a in artists)
        description = (
            f"{time_collection.availability_announcement_copy(month)}\n\n"
            f"{time_collection.availability_selection_sop()}\n\n"
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
        await interaction.edit_original_response(
            content=f"✅ 已建立 **{month}** 月度可配合時段收集。\n{where}{note}"
        )

    # ---------- /從時間池建立任務 ----------
    @app_commands.command(name="從時間池建立任務", description="從月度時間池找共同空檔並建立安排")
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

    @app_commands.command(name="正式通告發布", description="預覽並發布已有明確時間的正式 Task")
    @app_commands.default_permissions(administrator=True)
    @is_admin()
    async def publish_formal_notice(self, interaction: discord.Interaction):
        if not await channel_guard.ensure_schedule_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            tasks = [
                task for task in await repositories.get_active_tasks()
                if repositories.is_publishable_formal_task(task)
                and not formal_notice.was_published(task.page_id)
            ]
        except Exception:
            logger.exception("讀取正式通告候選 Task 失敗")
            await interaction.followup.send("❌ 讀取正式 Task 失敗，請稍後再試。", ephemeral=True)
            return
        tasks.sort(key=lambda task: task.start or datetime.max.replace(tzinfo=config.TZ))
        if not tasks:
            await interaction.followup.send(
                "目前沒有可發布的正式 Task。請確認 Task 已關聯藝人，且硬性安排有明確開始與截止時間。",
                ephemeral=True,
            )
            return
        limit_note = "\n⚠️ 目前先顯示最早的 25 筆。" if len(tasks) > 25 else ""
        await interaction.followup.send(
            "請選擇要預覽的正式 Task。確認前不會發送任何通知。" + limit_note,
            view=FormalNoticeTaskSelectView(tasks[:25], interaction.user),
            ephemeral=True,
        )

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
