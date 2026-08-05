"""藝人端：填寫可用時間按鈕（persistent）、時段 Modal、/我的時段。"""
import logging
import re
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs import channel_guard
from services import audit, repositories, timeparse
from services import time_collection

logger = logging.getLogger(__name__)

INTERACTION_VIEW_TIMEOUT_SECONDS = 7200


class InteractionDebugView(discord.ui.View):
    def __init__(self, timeout: int | None = INTERACTION_VIEW_TIMEOUT_SECONDS):
        super().__init__(timeout=timeout)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ):
        logger.error(
            "Discord UI 互動失敗｜user=%s｜item=%s｜view=%s",
            getattr(interaction.user, "id", "unknown"),
            item,
            type(self).__name__,
            exc_info=(type(error), error, error.__traceback__),
        )
        message = "❌ 這個互動已失效或執行失敗，請從公告的「選擇可配合時段」重新進入一次。"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            logger.exception("回覆 Discord UI 錯誤訊息失敗")


def _future_slots(slots: list[repositories.AvailabilitySlot]) -> list[repositories.AvailabilitySlot]:
    now = datetime.now(config.TZ)
    return [slot for slot in slots if slot.end and slot.end > now]


async def _cleanup_artist_time_pool(
    artist: repositories.Artist,
    operator: str,
    discord_user_id: str,
) -> tuple[repositories.AvailabilitySlotCompaction, int]:
    slots = _future_slots(await repositories.get_slots_for_artist(artist.page_id))
    compaction = await repositories.apply_availability_slot_compaction(slots)
    cleanup_count = len(compaction.redundant) + len(compaction.updates)
    if cleanup_count:
        await audit.log_event(
            event_name=f"整理重複可用時段｜{artist.name}",
            action_type="更新",
            operator=operator,
            discord_user_id=discord_user_id,
            target_db="Availability Slots",
            before=f"{len(slots)} 筆",
            after=(
                f"更新 {len(compaction.updates)} 筆合併時段，"
                f"封存 {len(compaction.redundant)} 筆冗餘時段"
            ),
        )
    return compaction, cleanup_count


async def _cleanup_notice(artist: repositories.Artist, interaction: discord.Interaction) -> str:
    try:
        _, cleanup_count = await _cleanup_artist_time_pool(
            artist, artist.name, str(interaction.user.id)
        )
    except Exception:
        logger.exception("送出後自動整理時間池失敗")
        return "\n⚠️ 時間已記錄，但自動整理時間池失敗，請稍後使用 `/我的時段` 或請營運執行 `/時間池整理`。"
    if cleanup_count:
        return f"\n\n已自動整理 {cleanup_count} 筆重複、重疊或冗餘時段。"
    return ""


async def _cleanup_notice_for_user(interaction: discord.Interaction) -> str:
    """按「完成」時才整理一次時間池（改成批次，避免每填一筆就整理造成 Notion 尖峰）。"""
    artist = await repositories.find_artist_by_discord_id(interaction.user.id)
    if artist is None:
        return ""
    return await _cleanup_notice(artist, interaction)


def _append_note_part(note_parts: list[str], value: str | None):
    if value:
        note_parts.append(value)


def _warning_label(warning: repositories.ScheduleWarning) -> str:
    prefix = "專案" if warning.source_type == "Project" else "任務"
    return f"{prefix}：{warning.source_name}"


def _unique_warnings(
    warnings: list[repositories.ScheduleWarning],
) -> list[repositories.ScheduleWarning]:
    seen: set[str] = set()
    unique: list[repositories.ScheduleWarning] = []
    for warning in warnings:
        if warning.source_id in seen:
            continue
        seen.add(warning.source_id)
        unique.append(warning)
    return unique


def _warning_note(warnings: list[repositories.ScheduleWarning]) -> str:
    unique_warnings = _unique_warnings(warnings)
    if not unique_warnings:
        return ""
    labels = [_warning_label(warning) for warning in unique_warnings[:5]]
    if len(unique_warnings) > 5:
        labels.append(f"另有 {len(unique_warnings) - 5} 筆")
    return "既有任務提醒：" + " / ".join(labels)


def _warning_text(warnings: list[repositories.ScheduleWarning]) -> str:
    unique_warnings = _unique_warnings(warnings)
    if not unique_warnings:
        return ""
    labels = [_warning_label(warning) for warning in unique_warnings[:5]]
    if len(unique_warnings) > 5:
        labels.append(f"另有 {len(unique_warnings) - 5} 筆")
    return "\n⚠️ 這些日期已有任務或專案，已寫入 Notion 備註供營運判斷：\n" + "\n".join(
        f"・{label}" for label in labels
    )


def _conflict_text(conflicts: list[repositories.ScheduleConflict]) -> str:
    if not conflicts:
        return ""
    seen: set[tuple[str, datetime, datetime]] = set()
    unique: list[repositories.ScheduleConflict] = []
    for conflict in conflicts:
        key = (conflict.source_id, conflict.start, conflict.end)
        if key in seen:
            continue
        seen.add(key)
        unique.append(conflict)
    lines: list[str] = []
    for conflict in unique[:8]:
        lines.append(f"・{conflict.source_name}｜{timeparse.humanize(conflict.start, conflict.end)}")
    suffix = f"\n・另有 {len(unique) - len(lines)} 筆衝突" if len(unique) > len(lines) else ""
    return "\n⚠️ 已排除與正式安排衝突的時間：\n" + "\n".join(lines) + suffix


async def _schedule_impact_context(
    artist: repositories.Artist,
) -> repositories.ScheduleImpactContext:
    return await repositories.get_schedule_impact_context_for_artist(artist.page_id)


def _available_windows_with_impact(
    start: datetime,
    end: datetime,
    context: repositories.ScheduleImpactContext,
) -> tuple[
    list[tuple[datetime, datetime]],
    list[repositories.ScheduleConflict],
    list[repositories.ScheduleWarning],
]:
    conflicts = repositories.conflicts_for_window(start, end, context.busy_intervals)
    windows = repositories.split_availability_by_busy_intervals(start, end, context.busy_intervals)
    warnings = repositories.warnings_for_window(start, end, context.warnings)
    return windows, conflicts, warnings


def _slot_note(base_parts: list[str], warnings: list[repositories.ScheduleWarning]) -> str:
    note_parts = list(base_parts)
    _append_note_part(note_parts, _warning_note(warnings))
    return "｜".join(note_parts)


_sent_pool_artist_done_notices: set[str] = set()


def _name_list(names: list[str]) -> str:
    return "、".join(names) if names else "無"


def pool_artist_done_notice(
    month: str,
    artist: repositories.Artist,
    artists: list[repositories.Artist],
    progress: repositories.MonthlyPoolProgress,
) -> str:
    name_of = {item.page_id: item.name for item in artists}
    missing_names = [name_of[page_id] for page_id in progress.missing_artist_page_ids if page_id in name_of]
    filled_count = len(progress.filled_artist_page_ids)
    lines = [
        f"🟢 **{artist.name} 已完成 {month} 月度可配合時段回填**",
        f"目前進度：{filled_count}/{len(artists)} 位",
    ]
    if missing_names:
        lines.append(f"未填：{_name_list(missing_names)}")
    return "\n".join(lines)


def pool_all_done_notice(month: str, artists: list[repositories.Artist]) -> str:
    names = _name_list([artist.name for artist in artists])
    return (
        f"✅ **{month} 月度時間池已收齊**\n"
        f"已填藝人：{names}\n"
        f"可以開始使用 `/時間池比對 月份:{month} 時長分鐘:60` 查看共同空檔。"
    )


def _collection_for(month: str, deadline_ts: int) -> time_collection.ActiveCollection | None:
    for collection in time_collection.active_collections():
        if collection.month == month and collection.deadline_ts == deadline_ts:
            return collection
    return None


async def _pool_collection_artists(
    month: str,
    deadline_ts: int,
) -> list[repositories.Artist]:
    collection = _collection_for(month, deadline_ts)
    if collection:
        return await repositories.get_artists_by_ids(list(collection.artist_page_ids))
    return await repositories.get_active_artists()


async def _send_pool_completion_notifications(
    interaction: discord.Interaction,
    artist: repositories.Artist,
    month: str,
    deadline_ts: int,
) -> None:
    admin_channel = interaction.client.get_channel(config.ADMIN_NOTIFY_CHANNEL_ID) \
        or interaction.client.get_channel(config.SCHEDULE_CHANNEL_ID)
    if not admin_channel:
        return
    try:
        artists = await _pool_collection_artists(month, deadline_ts)
        expected_ids = [item.page_id for item in artists]
        progress = await repositories.get_monthly_pool_progress(month, expected_ids)
    except Exception:
        logger.exception("月度時間池完成通知計算失敗")
        return

    artist_notice_key = f"{month}:{deadline_ts}:{artist.page_id}"
    if artist_notice_key not in _sent_pool_artist_done_notices:
        await admin_channel.send(pool_artist_done_notice(month, artist, artists, progress))
        _sent_pool_artist_done_notices.add(artist_notice_key)

    if progress.is_complete and time_collection.collection_notice_once(month, deadline_ts, "all_done"):
        await admin_channel.send(pool_all_done_notice(month, artists))


class PoolAvailabilityModal(discord.ui.Modal):
    """月度可配合時段收集表單，不綁定單一排課需求。"""

    def __init__(self, month: str, deadline_ts: int, nature_code: str):
        super().__init__(title=f"填寫可配合時段｜{month}")
        self.month = month
        self.deadline_ts = deadline_ts
        self.nature_code = nature_code
        self.date_input = discord.ui.TextInput(
            label="日期（例：8/5 或 2026-08-05）", placeholder="8/5", max_length=12
        )
        self.start_input = discord.ui.TextInput(
            label="開始時間（例：14:00）", placeholder="14:00", max_length=8
        )
        self.end_input = discord.ui.TextInput(
            label="結束時間（例：18:00，可跨日）", placeholder="18:00", max_length=8
        )
        self.note_input = discord.ui.TextInput(
            label="備註（選填：限制、偏好或不方便事項）", required=False, max_length=100
        )
        for item in (self.date_input, self.start_input, self.end_input, self.note_input):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            start, end = timeparse.parse_slot(
                self.date_input.value, self.start_input.value, self.end_input.value
            )
        except timeparse.ParseError as e:
            await interaction.followup.send(
                f"❌ {e}\n請再按一次「填寫可配合時段」重新輸入。", ephemeral=True
            )
            return

        artist = await repositories.find_artist_by_discord_id(interaction.user.id)
        if artist is None:
            await interaction.followup.send(
                "❌ 找不到你的藝人資料，請聯絡管理者確認 Notion 上的 Discord User ID 設定。",
                ephemeral=True,
            )
            return
        try:
            context = await _schedule_impact_context(artist)
        except Exception:
            logger.exception("讀取正式安排衝突資料失敗")
            await interaction.followup.send(
                "❌ 讀不到你已安排的正式任務，暫時不能填寫，請稍後再試或聯絡管理者。",
                ephemeral=True,
            )
            return

        windows, conflicts, warnings = _available_windows_with_impact(start, end, context)
        if not windows:
            await interaction.followup.send(
                "⚠️ 你填寫的時段已被正式安排占用，未新增可配合時間。"
                + _conflict_text(conflicts),
                ephemeral=True,
            )
            return

        deadline = datetime.fromtimestamp(self.deadline_ts, tz=config.TZ)
        is_late = datetime.now(config.TZ) > deadline
        nature_label = time_collection.collection_nature_label(self.nature_code)
        note_parts = [f"收集月份：{self.month}", f"收集性質：{nature_label}"]
        if self.note_input.value:
            note_parts.append(f"藝人備註：{self.note_input.value}")
        try:
            existing_slots = await repositories.get_slots_for_artist(artist.page_id)
            created_slots: list[repositories.AvailabilitySlot] = []
            duplicates: list[repositories.AvailabilitySlot] = []
            for window_start, window_end in windows:
                slot, created = await repositories.create_availability_slot_if_absent(
                    artist=artist,
                    start=window_start,
                    end=window_end,
                    is_late=is_late,
                    note=_slot_note(note_parts, warnings),
                    existing_slots=existing_slots,
                )
                if created:
                    created_slots.append(slot)
                else:
                    duplicates.append(slot)
        except Exception:
            logger.exception("建立月度可配合時段失敗")
            await interaction.followup.send("❌ 寫入 Notion 失敗，請稍後再試或聯絡管理者。", ephemeral=True)
            await audit.log_event(
                event_name=f"填寫月度可配合時段失敗｜{artist.name}",
                action_type="錯誤", operator=artist.name,
                discord_user_id=str(interaction.user.id),
                target_db="Availability Slots", result="失敗",
                error_message="create_availability_slot exception（詳見 bot log）",
            )
            return

        if not created_slots:
            duplicate_lines = [
                f"・{timeparse.humanize(slot.start, slot.end)}"
                for slot in duplicates[:8]
                if slot.start and slot.end
            ]
            await interaction.followup.send(
                f"⚠️ 這些月度可配合時段已經存在，沒有重複新增：{len(duplicates)} 筆\n"
                + "\n".join(duplicate_lines)
                + _conflict_text(conflicts)
                + _warning_text(warnings)
                + "\n\n若要修改，請用 `/我的時段` 刪除後，再用 `/補填時間` 重新填寫。",
                view=PoolAfterSubmitView(self.month, self.deadline_ts, self.nature_code),
                ephemeral=True,
            )
            return

        created_text = "\n".join(
            f"・{timeparse.humanize(slot.start, slot.end)}"
            for slot in created_slots
            if slot.start and slot.end
        )
        await audit.log_event(
            event_name=f"填寫月度可配合時段｜{artist.name}｜{self.month}",
            action_type="提交", operator=artist.name,
            discord_user_id=str(interaction.user.id),
            target_db="Availability Slots",
            after=f"{len(created_slots)} 筆",
        )
        late_note = "\n⚠️ 已超過填寫截止時間，此筆標記為逾期提交。" if is_late else ""
        await interaction.followup.send(
            "✅ 已記錄你的月度可配合時段：\n"
            + created_text
            + _conflict_text(conflicts)
            + _warning_text(warnings)
            + late_note
            + "\n\n這不是正式行程；正式安排前營運仍會再確認。",
            view=PoolAfterSubmitView(self.month, self.deadline_ts, self.nature_code),
            ephemeral=True,
        )


class PoolAfterSubmitView(InteractionDebugView):
    """月度可配合時段送出後的後續動作。"""

    def __init__(self, month: str, deadline_ts: int, nature_code: str):
        super().__init__()
        self.month = month
        self.deadline_ts = deadline_ts
        self.nature_code = nature_code

    @discord.ui.button(label="再填一筆", style=discord.ButtonStyle.primary, emoji="➕")
    async def add_more(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(
            PoolAvailabilityModal(self.month, self.deadline_ts, self.nature_code)
        )

    @discord.ui.button(label="完成", style=discord.ButtonStyle.secondary, emoji="✅")
    async def done(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="⏳ 正在整理你的時間池…", view=None)
        cleanup_text = await _cleanup_notice_for_user(interaction)
        artist = await repositories.find_artist_by_discord_id(interaction.user.id)
        if artist is not None:
            await _send_pool_completion_notifications(
                interaction, artist, self.month, self.deadline_ts
            )
        await interaction.edit_original_response(
            content="✅ 感謝填寫！月度可配合時段已記錄，營運會用來初步安排課程 / 錄音 / 通告；正式安排前仍會再確認。" + cleanup_text
        )


class WeekSelect(discord.ui.Select):
    def __init__(
        self,
        month: str,
        deadline_ts: int,
        nature_code: str,
    ):
        self.month = month
        self.deadline_ts = deadline_ts
        self.nature_code = nature_code
        self.week_options = time_collection.month_week_options(month)
        options = [
            discord.SelectOption(label=week.label, value=week.value)
            for week in self.week_options
        ]
        super().__init__(
            placeholder="先選週次（可多選）…",
            min_values=1,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        day_options = time_collection.week_values_day_options(list(self.values))
        week_label_by_value = {week.value: week.label for week in self.week_options}
        selected_week_labels = "、".join(week_label_by_value[value] for value in self.values)
        if not day_options:
            await interaction.response.edit_message(
                content=(
                    f"已選週次：**{selected_week_labels}**\n"
                    "這些週次沒有可選日期，請改選其他週次。"
                ),
                view=WeekSelectView(self.month, self.deadline_ts, self.nature_code),
            )
            return
        view = DatePresetView(
            self.month,
            self.deadline_ts,
            list(self.values),
            self.nature_code,
        )
        await interaction.response.edit_message(
            content=(
                f"已選週次：**{selected_week_labels}**\n"
                f"可回報 **{len(day_options)} 天**。\n"
                f"{view.prompt()}"
            ),
            view=view,
        )


class WeekSelectView(InteractionDebugView):
    def __init__(
        self,
        month: str,
        deadline_ts: int,
        nature_code: str,
    ):
        super().__init__()
        self.month = month
        self.deadline_ts = deadline_ts
        self.nature_code = nature_code
        self.add_item(WeekSelect(month, deadline_ts, nature_code))

    @discord.ui.button(label="自訂時間 / 備註", style=discord.ButtonStyle.secondary, emoji="✏️")
    async def custom_time(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(
            PoolAvailabilityModal(self.month, self.deadline_ts, self.nature_code)
        )


class DaySelect(discord.ui.Select):
    def __init__(self, parent_view: "DatePresetView"):
        self.parent_view = parent_view
        options = [
            discord.SelectOption(
                label=day.label,
                value=day.value,
                default=day.value in parent_view.day_values,
            )
            for day in parent_view.current_day_options
        ]
        super().__init__(
            placeholder="選擇可配合日期（可多選）…",
            row=0,
            min_values=1,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.update_day_values(list(self.values))
        view = self.parent_view.page_view(self.parent_view.page_index)
        await interaction.response.edit_message(content=view.prompt(), view=view)


async def _submit_pool_slots(
    interaction: discord.Interaction,
    month: str,
    deadline_ts: int,
    nature_code: str,
    week_values: list[str],
    day_values: list[str],
    mode: str,
    start_text: str = "",
    end_text: str = "",
):
    await interaction.response.defer(ephemeral=True, thinking=True)
    artist = await repositories.find_artist_by_discord_id(interaction.user.id)
    if artist is None:
        await interaction.followup.send(
            "❌ 找不到你的藝人資料，請聯絡管理者確認 Notion 上的 Discord User ID 設定。",
            ephemeral=True,
        )
        return
    try:
        context = await _schedule_impact_context(artist)
    except Exception:
        logger.exception("讀取正式安排衝突資料失敗")
        await interaction.followup.send(
            "❌ 讀不到你已安排的正式任務，暫時不能送出，請稍後再試或聯絡管理者。",
            ephemeral=True,
        )
        return

    try:
        slots = time_collection.build_mode_slots(
            day_values, mode, start_text=start_text, end_text=end_text
        )
    except (ValueError, timeparse.ParseError) as e:
        await interaction.followup.send(f"❌ {e}", ephemeral=True)
        return

    deadline = datetime.fromtimestamp(deadline_ts, tz=config.TZ)
    now = datetime.now(config.TZ)
    is_late = now > deadline
    nature_label = time_collection.collection_nature_label(nature_code)
    existing_slots = await repositories.get_slots_for_artist(artist.page_id)
    created: list[str] = []
    duplicates: list[str] = []
    skipped: list[str] = []
    conflicts: list[repositories.ScheduleConflict] = []
    warnings: list[repositories.ScheduleWarning] = []
    for start, end in slots:
        if end <= now:
            skipped.append(start.strftime("%m/%d"))
            continue
        windows, slot_conflicts, slot_warnings = _available_windows_with_impact(start, end, context)
        conflicts.extend(slot_conflicts)
        warnings.extend(slot_warnings)
        try:
            for window_start, window_end in windows:
                if window_end <= now:
                    skipped.append(window_start.strftime("%m/%d"))
                    continue
                slot, did_create = await repositories.create_availability_slot_if_absent(
                    artist=artist,
                    start=window_start,
                    end=window_end,
                    is_late=is_late,
                    note=_slot_note(
                        [f"收集月份：{month}", f"收集性質：{nature_label}"],
                        slot_warnings,
                    ),
                    existing_slots=existing_slots,
                )
                if did_create:
                    created.append(timeparse.humanize(window_start, window_end))
                else:
                    duplicates.append(timeparse.humanize(slot.start, slot.end))
        except Exception:
            logger.exception("建立月度可配合時段失敗")
            await interaction.followup.send("❌ 寫入 Notion 失敗，請稍後再試或聯絡管理者。", ephemeral=True)
            return

    if created:
        await audit.log_event(
            event_name=f"批次填寫月度可配合時段｜{artist.name}｜{month}",
            action_type="提交",
            operator=artist.name,
            discord_user_id=str(interaction.user.id),
            target_db="Availability Slots",
            after=f"{len(created)} 筆",
        )
    skipped_text = f"\n⚠️ 已略過過去日期：{ '、'.join(skipped) }" if skipped else ""
    duplicate_text = ""
    if duplicates:
        duplicate_text = (
            f"\n⚠️ 已存在、未重複新增：{len(duplicates)} 筆\n"
            + "\n".join(f"・{line}" for line in duplicates[:10])
        )
        if len(duplicates) > 10:
            duplicate_text += f"\n・另有 {len(duplicates) - 10} 筆已存在"
    late_text = "\n⚠️ 已超過填寫截止時間，這些時段標記為逾期提交。" if is_late and created else ""
    if not created:
        await interaction.followup.send(
            "⚠️ 沒有新增新的未來時段。"
            + duplicate_text
            + skipped_text
            + _conflict_text(conflicts)
            + _warning_text(warnings)
            + "\n\n若要修改，請用 `/我的時段` 刪除後，再用 `/補填時間` 重新填寫。",
            view=PoolContinueView(month, deadline_ts, nature_code, week_values),
            ephemeral=True,
        )
        return
    await interaction.followup.send(
        "✅ 已記錄以下月度可配合時段：\n"
        + "\n".join(f"・{line}" for line in created)
        + duplicate_text
        + skipped_text
        + _conflict_text(conflicts)
        + _warning_text(warnings)
        + late_text
        + "\n\n這不是正式行程；正式安排前營運仍會再確認。"
        + "\n可以繼續選下一批日期 / 可用模式，或按完成。",
        view=PoolContinueView(month, deadline_ts, nature_code, week_values),
        ephemeral=True,
    )


class ModeTimeModal(discord.ui.Modal):
    def __init__(
        self,
        month: str,
        deadline_ts: int,
        nature_code: str,
        week_values: list[str],
        day_values: list[str],
        mode: str,
    ):
        super().__init__(title="補充可用時間")
        self.month = month
        self.deadline_ts = deadline_ts
        self.nature_code = nature_code
        self.week_values = week_values
        self.day_values = day_values
        self.mode = mode
        self.start_input: discord.ui.TextInput | None = None
        self.end_input: discord.ui.TextInput | None = None
        if mode in {"after", "specific"}:
            self.start_input = discord.ui.TextInput(
                label="開始時間（例：13:30）",
                placeholder="13:30",
                max_length=8,
            )
            self.add_item(self.start_input)
        if mode in {"before", "specific"}:
            self.end_input = discord.ui.TextInput(
                label="結束時間（例：17:00）",
                placeholder="17:00",
                max_length=8,
            )
            self.add_item(self.end_input)

    async def on_submit(self, interaction: discord.Interaction):
        await _submit_pool_slots(
            interaction,
            self.month,
            self.deadline_ts,
            self.nature_code,
            self.week_values,
            self.day_values,
            self.mode,
            start_text=self.start_input.value if self.start_input else "",
            end_text=self.end_input.value if self.end_input else "",
        )


class ApplyModeButton(discord.ui.Button):
    def __init__(
        self,
        parent_view: "DatePresetView",
        label: str,
        mode: str,
        *,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    ):
        super().__init__(
            label=label,
            style=style,
            row=1,
            disabled=not parent_view.day_values,
        )
        self.parent_view = parent_view
        self.mode = mode

    async def callback(self, interaction: discord.Interaction):
        if not self.parent_view.day_values:
            await interaction.response.send_message("請先勾選要套用的日期。", ephemeral=True)
            return
        if self.mode == "all_day":
            await _submit_pool_slots(
                interaction,
                self.parent_view.month,
                self.parent_view.deadline_ts,
                self.parent_view.nature_code,
                self.parent_view.week_values,
                self.parent_view.day_values,
                self.mode,
            )
            return
        await interaction.response.send_modal(
            ModeTimeModal(
                self.parent_view.month,
                self.parent_view.deadline_ts,
                self.parent_view.nature_code,
                self.parent_view.week_values,
                self.parent_view.day_values,
                self.mode,
            )
        )


class DatePresetView(InteractionDebugView):
    def __init__(
        self,
        month: str,
        deadline_ts: int,
        week_values: list[str],
        nature_code: str,
        page_index: int = 0,
        selected_day_values: list[str] | None = None,
        selected_mode: str | None = None,
    ):
        super().__init__()
        self.month = month
        self.deadline_ts = deadline_ts
        self.week_values = week_values
        self.nature_code = nature_code
        # 正常流程 week_values 必非空；每頁 7 天，避免 Discord Select 25 個選項上限造成誤導。
        self.day_pages = time_collection.day_option_pages(week_values, page_size=7) or [[]]
        self.page_index = max(0, min(page_index, len(self.day_pages) - 1))
        self.current_day_options = self.day_pages[self.page_index]
        self.all_day_values = [
            option.value for page in self.day_pages for option in page
        ]
        self.day_values = self._sort_day_values(selected_day_values or [])
        self.mode = selected_mode
        if self.current_day_options:
            self.add_item(DaySelect(self))
        self.add_item(
            ApplyModeButton(
                self,
                "整天可",
                "all_day",
                style=discord.ButtonStyle.primary,
            )
        )
        self.add_item(ApplyModeButton(self, "某時間後", "after"))
        self.add_item(ApplyModeButton(self, "某時間前", "before"))
        self.add_item(ApplyModeButton(self, "指定時段", "specific"))
        if len(self.day_pages) > 1:
            prev_button = discord.ui.Button(
                label="上一頁",
                style=discord.ButtonStyle.secondary,
                disabled=self.page_index == 0,
                row=3,
            )
            next_button = discord.ui.Button(
                label="下一頁",
                style=discord.ButtonStyle.secondary,
                disabled=self.page_index >= len(self.day_pages) - 1,
                row=3,
            )
            prev_button.callback = self.previous_page
            next_button.callback = self.next_page
            self.add_item(prev_button)
            self.add_item(next_button)

    def _sort_day_values(self, values: list[str]) -> list[str]:
        value_set = set(values)
        return [value for value in self.all_day_values if value in value_set]

    def _all_days_selected(self) -> bool:
        return bool(self.all_day_values) and len(self.day_values) == len(self.all_day_values)

    def update_day_values(self, page_values: list[str]):
        current_values = {option.value for option in self.current_day_options}
        next_values = [
            value for value in self.day_values
            if value not in current_values
        ] + page_values
        self.day_values = self._sort_day_values(next_values)

    def prompt(self) -> str:
        page_count = len(self.day_pages)
        page_days = "、".join(day.label for day in self.current_day_options)
        selected_hint = time_collection.availability_page_hint(len(self.day_values), self.mode)
        return (
            f"勾同一批日期，套用同一個時段：\n"
            f"日期頁：**{self.page_index + 1}/{page_count}**｜"
            f"本頁：{page_days}\n"
            f"已勾 **{len(self.day_values)} 天**。{selected_hint}\n"
            "日期時間不同時，分批勾選；同一天有多段時間，就再勾同一天填下一段。"
        )

    def page_view(self, page_index: int) -> "DatePresetView":
        return DatePresetView(
            self.month,
            self.deadline_ts,
            self.week_values,
            self.nature_code,
            page_index=page_index,
            selected_day_values=self.day_values,
            selected_mode=self.mode,
        )

    async def previous_page(self, interaction: discord.Interaction):
        view = self.page_view(self.page_index - 1)
        await interaction.response.edit_message(
            content=view.prompt(),
            view=view,
        )

    async def next_page(self, interaction: discord.Interaction):
        view = self.page_view(self.page_index + 1)
        await interaction.response.edit_message(
            content=view.prompt(),
            view=view,
        )

    @discord.ui.button(label="改選週次", style=discord.ButtonStyle.secondary, emoji="↩️", row=4)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(
            content=f"請先選擇 **{self.month}** 要填寫的週次：",
            view=WeekSelectView(self.month, self.deadline_ts, self.nature_code),
        )

    @discord.ui.button(label="自訂時間 / 備註", style=discord.ButtonStyle.secondary, emoji="✏️", row=4)
    async def custom_time(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(
            PoolAvailabilityModal(self.month, self.deadline_ts, self.nature_code)
        )


class PoolContinueView(InteractionDebugView):
    def __init__(
        self,
        month: str,
        deadline_ts: int,
        nature_code: str,
        week_values: list[str] | None = None,
    ):
        super().__init__()
        self.month = month
        self.deadline_ts = deadline_ts
        self.nature_code = nature_code
        self.week_values = week_values

    @discord.ui.button(label="繼續選下一組日期", style=discord.ButtonStyle.primary, emoji="➕")
    async def continue_selecting(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self.week_values:
            view = DatePresetView(
                self.month,
                self.deadline_ts,
                self.week_values,
                self.nature_code,
            )
            await interaction.response.edit_message(
                content=view.prompt(),
                view=view,
            )
            return
        await interaction.response.edit_message(
            content=f"請選擇 **{self.month}** 下一批要填寫的週次：",
            view=WeekSelectView(self.month, self.deadline_ts, self.nature_code),
        )

    @discord.ui.button(label="自訂時間 / 備註", style=discord.ButtonStyle.secondary, emoji="✏️")
    async def custom_time(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(
            PoolAvailabilityModal(self.month, self.deadline_ts, self.nature_code)
        )

    @discord.ui.button(label="完成", style=discord.ButtonStyle.secondary, emoji="✅")
    async def done(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="⏳ 正在整理你的時間池…", view=None)
        cleanup_text = await _cleanup_notice_for_user(interaction)
        artist = await repositories.find_artist_by_discord_id(interaction.user.id)
        if artist is not None:
            await _send_pool_completion_notifications(
                interaction, artist, self.month, self.deadline_ts
            )
        await interaction.edit_original_response(
            content="✅ 感謝填寫！月度可配合時段已記錄，營運會用來初步安排課程 / 錄音 / 通告；正式安排前仍會再確認。" + cleanup_text
        )


class PoolAvailabilityButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"pool_avail:(?P<month>\d{4}-\d{2}):(?P<deadline_ts>\d+)(?::(?P<nature_code>[a-z_]+))?",
):
    """月度時間收集公告上的「選擇可配合時段」按鈕，bot 重啟後仍有效。"""

    def __init__(
        self,
        month: str,
        deadline_ts: int,
        nature_code: str = time_collection.DEFAULT_COLLECTION_NATURE,
    ):
        self.month = month
        self.deadline_ts = int(deadline_ts)
        self.nature_code = nature_code
        super().__init__(
            discord.ui.Button(
                label="📅 選擇可配合時段",
                style=discord.ButtonStyle.success,
                custom_id=f"pool_avail:{month}:{self.deadline_ts}:{nature_code}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        nature_code = match.groupdict().get("nature_code") or time_collection.DEFAULT_COLLECTION_NATURE
        return cls(match["month"], int(match["deadline_ts"]), nature_code)

    async def callback(self, interaction: discord.Interaction):
        artist = await repositories.find_artist_by_discord_id(interaction.user.id)
        if artist is None:
            await interaction.response.send_message(
                "❌ 找不到你的藝人資料，請聯絡管理者確認 Notion 上的 Discord User ID 設定。",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"回報 **{self.month} 可配合時間**。\n"
            "先選週次，再勾同一批日期套用同一段時間；送出時會自動檢查已安排任務，若有衝突只排除衝突時段。\n\n"
            f"{time_collection.availability_selection_sop()}",
            view=WeekSelectView(self.month, self.deadline_ts, self.nature_code),
            ephemeral=True,
        )


def make_pool_availability_view(month: str, deadline: datetime, nature_code: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(PoolAvailabilityButton(month, int(deadline.timestamp()), nature_code))
    return view


def _slot_sort_key(slot: repositories.AvailabilitySlot) -> datetime:
    return slot.start or datetime.max.replace(tzinfo=config.TZ)


def _slot_month_label(slot: repositories.AvailabilitySlot) -> str:
    if slot.note:
        month = time_collection.note_field(slot.note, "收集月份")
        if month:
            return month
    if slot.start:
        return slot.start.strftime("%Y-%m")
    return "未分類"


def _slot_line(slot: repositories.AvailabilitySlot) -> str:
    if not slot.start or not slot.end:
        return slot.name or "（未命名時段）"
    line = time_collection.format_slot_summary(slot.start, slot.end, slot.note)
    artist_note = time_collection.note_field(slot.note, "藝人備註") if slot.note else None
    if artist_note:
        line += f"｜備註：{artist_note}"
    return line


def _slot_delete_label(slot: repositories.AvailabilitySlot) -> str:
    if slot.start and slot.end:
        return time_collection.format_slot_summary(slot.start, slot.end, slot.note)[:100]
    return (slot.name or "（未命名時段）")[:100]


def _trim_discord_message(text: str, limit: int = 1900) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 18].rstrip() + "\n…清單過長已截斷"


class MySlotsDeleteView(InteractionDebugView):
    """列出自己已填時段，可多選刪除（Notion archive）。"""

    def __init__(self, slots: list[repositories.AvailabilitySlot], artist: repositories.Artist):
        super().__init__()
        self.artist = artist
        self.slot_by_id = {slot.page_id: slot for slot in slots[:25]}
        options = [
            discord.SelectOption(
                label=_slot_delete_label(slot),
                value=slot.page_id,
                description=_slot_month_label(slot)[:100],
            )
            for slot in slots[:25]
        ]
        self.select = discord.ui.Select(
            placeholder="選擇要刪除的時段（可多選）…",
            min_values=1,
            max_values=min(25, len(options)),
            options=options,
        )
        self.select.callback = self.on_delete
        self.add_item(self.select)

    async def on_delete(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        page_ids = list(self.select.values)
        labels = [
            _slot_delete_label(self.slot_by_id[page_id])
            for page_id in page_ids
            if page_id in self.slot_by_id
        ]
        try:
            for page_id in page_ids:
                await repositories.archive_slot(page_id)
        except Exception:
            logger.exception("刪除時段失敗")
            await interaction.followup.send("❌ 刪除失敗，請稍後再試。", ephemeral=True)
            return
        await audit.log_event(
            event_name=f"刪除可用時段｜{self.artist.name}",
            action_type="更新", operator=self.artist.name,
            discord_user_id=str(interaction.user.id),
            target_db="Availability Slots",
            before=f"{len(page_ids)} 筆",
            after="（已刪除）",
        )
        await interaction.followup.send(
            "🗑️ 已刪除以下時段，Notion 端也會同步移除：\n"
            + "\n".join(f"・{label}" for label in labels)
            + "\n\n可直接使用 `/補填時間` 重新填寫，不需要回去找舊公告。",
            ephemeral=True,
        )


class SchedulingArtistCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="補填時間", description="不用找舊公告，直接補填指定月份的月度可配合時間")
    @app_commands.describe(月份="要補填的月份，例如 2026-08")
    async def refill_time(self, interaction: discord.Interaction, 月份: str):
        if not await channel_guard.ensure_schedule_channel(interaction):
            return
        normalized_month = time_collection.normalize_month(月份)
        if normalized_month is None:
            await interaction.response.send_message(
                "❌ 月份格式不正確，請輸入像 `2026-08` 這樣的格式。",
                ephemeral=True,
            )
            return
        artist = await repositories.find_artist_by_discord_id(interaction.user.id)
        if artist is None:
            await interaction.response.send_message(
                "❌ 找不到你的藝人資料，請聯絡管理者確認 Notion 設定。",
                ephemeral=True,
            )
            return
        deadline_ts = int((datetime.now(config.TZ) + timedelta(days=3650)).timestamp())
        await interaction.response.send_message(
            f"補填 **{normalized_month} 可配合時間**。\n"
            "不用找舊公告；如果要修改已填資料，請先用 `/我的時段` 刪除錯的時段，再從這裡重填。\n\n"
            f"{time_collection.availability_selection_sop()}",
            view=WeekSelectView(
                normalized_month,
                deadline_ts,
                time_collection.DEFAULT_COLLECTION_NATURE,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="我的時段", description="查看並管理我已填寫的可用時間")
    async def my_slots(self, interaction: discord.Interaction):
        if not await channel_guard.ensure_schedule_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        artist = await repositories.find_artist_by_discord_id(interaction.user.id)
        if artist is None:
            await interaction.followup.send(
                "❌ 找不到你的藝人資料，請聯絡管理者確認 Notion 設定。", ephemeral=True
            )
            return
        now = datetime.now(config.TZ)
        all_slots = await repositories.get_slots_for_artist(artist.page_id)
        my_slots = [
            slot for slot in all_slots
            if slot.end and slot.end > now
        ]
        compaction = repositories.compact_availability_slots(my_slots)
        cleanup_count = len(compaction.redundant) + len(compaction.updates)
        if cleanup_count:
            try:
                compaction, cleanup_count = await _cleanup_artist_time_pool(
                    artist, artist.name, str(interaction.user.id)
                )
            except Exception:
                logger.exception("整理重複 / 冗餘時段失敗")
                await interaction.followup.send(
                    "⚠️ 偵測到重複、重疊或被較長時段包含的資料，但自動整理失敗，請稍後再試。",
                    ephemeral=True,
                )
                return
        my_slots = [
            slot for slot in compaction.visible
            if slot.end and slot.end > now
        ]
        my_slots = sorted(my_slots, key=_slot_sort_key)
        if not my_slots:
            cleanup_text = f"\n已自動整理 {cleanup_count} 筆重複、重疊或冗餘時段。" if cleanup_count else ""
            await interaction.followup.send(
                f"目前沒有已填寫的可用時段。{cleanup_text}",
                ephemeral=True,
            )
            return
        lines: list[str] = []
        lines.append("**月度可配合時段（不是正式行程）**")
        current_month = ""
        for slot in my_slots:
            month = _slot_month_label(slot)
            if month != current_month:
                current_month = month
                lines.append(f"__{month}__")
            lines.append(f"・{_slot_line(slot)}")
        await interaction.followup.send(
            _trim_discord_message(
                "你目前填寫的可用時段：\n"
                + "\n".join(lines)
                + (
                    f"\n\n已自動整理 {cleanup_count} 筆重複、重疊或被較長時段包含的資料。"
                    if cleanup_count else ""
                )
                + "\n\n月度可配合時段只供營運初步排程參考，不是正式行程。"
                + "\n要取消或改時間：在下方選單勾選要刪除的時段，再用 `/補填時間` 重新填寫。"
            ),
            view=MySlotsDeleteView(my_slots, artist),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(SchedulingArtistCog(bot))
