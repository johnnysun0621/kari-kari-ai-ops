"""藝人端：填寫與管理可配合時間的按鈕、Modal 與統一指令。"""
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs import channel_guard
from services import audit, repositories, timeparse
from services import time_collection

logger = logging.getLogger(__name__)

INTERACTION_VIEW_TIMEOUT_SECONDS = 7200


@dataclass(frozen=True)
class AvailabilityDraftGroup:
    day_values: tuple[str, ...]
    mode: str
    start_text: str = ""
    end_text: str = ""

    def slots(self) -> list[tuple[datetime, datetime]]:
        return time_collection.build_mode_slots(
            list(self.day_values),
            self.mode,
            start_text=self.start_text,
            end_text=self.end_text,
        )


_pool_drafts: dict[tuple[int, str], list[AvailabilityDraftGroup]] = {}


def _draft_key(user_id: int, month: str, _deadline_ts: int) -> tuple[int, str]:
    return user_id, month


def _drafts_for(user_id: int, month: str, deadline_ts: int) -> list[AvailabilityDraftGroup]:
    return list(_pool_drafts.get(_draft_key(user_id, month, deadline_ts), []))


def _set_drafts(
    user_id: int,
    month: str,
    deadline_ts: int,
    groups: list[AvailabilityDraftGroup],
) -> None:
    key = _draft_key(user_id, month, deadline_ts)
    if groups:
        _pool_drafts[key] = list(groups)
    else:
        _pool_drafts.pop(key, None)


def _draft_slots(groups: list[AvailabilityDraftGroup]) -> list[tuple[datetime, datetime]]:
    return [slot for group in groups for slot in group.slots()]


def _draft_day_values(groups: list[AvailabilityDraftGroup]) -> set[str]:
    return {value for group in groups for value in group.day_values}


def _replace_draft_group(
    groups: list[AvailabilityDraftGroup],
    index: int,
    replacement: AvailabilityDraftGroup,
) -> list[AvailabilityDraftGroup]:
    if index < 0 or index >= len(groups):
        raise IndexError("Draft group no longer exists")
    updated = list(groups)
    updated[index] = replacement
    return updated


def _past_day_values(month: str, *, today: date | None = None) -> set[str]:
    """Return dates in the target month that have already ended in Taipei."""
    current_day = today or datetime.now(config.TZ).date()
    month_start = date.fromisoformat(f"{month}-01")
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    cutoff = min(max(current_day, month_start), next_month)
    values: set[str] = set()
    cursor = month_start
    while cursor < cutoff:
        values.add(cursor.isoformat())
        cursor += timedelta(days=1)
    return values


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
        return "\n⚠️ 時間已記錄，但自動整理時間池失敗；請通知控方稍後重新執行安排比對。"
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
        source_label = "任務" if conflict.source_type == "Task" else "安排"
        lines.append(
            f"・{timeparse.humanize(conflict.start, conflict.end)}｜"
            f"{source_label}：{conflict.source_name}"
        )
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


def _preview_availability_impact(
    slots: list[tuple[datetime, datetime]],
    context: repositories.ScheduleImpactContext,
    *,
    now: datetime | None = None,
) -> tuple[
    list[tuple[datetime, datetime]],
    list[repositories.ScheduleConflict],
    list[tuple[datetime, datetime]],
]:
    """Preview the exact windows that remain after fixed schedule conflicts."""
    now = now or datetime.now(config.TZ)
    adjusted: list[tuple[datetime, datetime]] = []
    conflicts: list[repositories.ScheduleConflict] = []
    blocked: list[tuple[datetime, datetime]] = []
    for start, end in slots:
        if end <= now:
            continue
        windows, slot_conflicts, _ = _available_windows_with_impact(start, end, context)
        future_windows = [(window_start, window_end) for window_start, window_end in windows if window_end > now]
        adjusted.extend(future_windows)
        conflicts.extend(slot_conflicts)
        if slot_conflicts and not future_windows:
            blocked.append((start, end))
    return adjusted, conflicts, blocked


def _slot_note(base_parts: list[str], warnings: list[repositories.ScheduleWarning]) -> str:
    note_parts = list(base_parts)
    _append_note_part(note_parts, _warning_note(warnings))
    return "｜".join(note_parts)


def _schedule_item_overlaps_month(
    start: datetime | None,
    end: datetime | None,
    month: str,
) -> bool:
    month_start = datetime.strptime(f"{month}-01", "%Y-%m-%d").date()
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    month_end = next_month - timedelta(days=1)
    item_start = (start or end).date() if (start or end) else None
    item_end = (end or start).date() if (start or end) else None
    return bool(item_start and item_end and item_start <= month_end and item_end >= month_start)


def _schedule_period_text(
    start: datetime | None,
    end: datetime | None,
    *,
    show_time: bool,
) -> str:
    if not start and not end:
        return "日期未設定"
    first = start or end
    last = end or start
    if show_time and start and end:
        if start.date() == end.date():
            return f"{start.month}/{start.day} {start:%H:%M}–{end:%H:%M}"
        return f"{start.month}/{start.day} {start:%H:%M}–{end.month}/{end.day} {end:%H:%M}"
    if first.date() == last.date():
        return f"{first.month}/{first.day}"
    return f"{first.month}/{first.day}–{last.month}/{last.day}"


def _artist_schedule_summary(
    month: str,
    context: repositories.ScheduleImpactContext,
) -> str:
    entries: list[tuple[datetime, str]] = []
    seen: set[str] = set()
    for item in context.busy_intervals:
        if item.source_id in seen or not _schedule_item_overlaps_month(item.start, item.end, month):
            continue
        seen.add(item.source_id)
        item_start = item.start or item.end
        entries.append(
            (
                item_start,
                f"・{_schedule_period_text(item.start, item.end, show_time=True)}｜"
                f"{('任務' if item.source_type == 'Task' else '專案')}：{item.source_name}",
            )
        )
    for item in context.warnings:
        if item.source_id in seen or not _schedule_item_overlaps_month(item.start, item.end, month):
            continue
        seen.add(item.source_id)
        item_start = item.start or item.end
        entries.append(
            (
                item_start,
                f"・{_schedule_period_text(item.start, item.end, show_time=False)}｜"
                f"{('任務' if item.source_type == 'Task' else '專案')}：{item.source_name}",
            )
        )
    if not entries:
        return "📌 本月目前沒有讀到既定安排。\n"
    entries.sort(key=lambda entry: (entry[0], entry[1]))
    lines = [
        f"📌 **你在 {month} 的既定安排**",
        "以下資訊供你判斷可配合時間；仍可填寫其他時段。",
    ]
    lines.append("\n".join(line for _, line in entries[:30]))
    if len(entries) > 30:
        lines.append(f"・另有 {len(entries) - 30} 筆既定安排")
    return "\n".join(lines) + "\n"


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
        f"可以開始使用 `/從時間池建立任務 月份:{month} 時長分鐘:60` 查看共同空檔。"
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
                + "\n\n若要修改，請使用 `/管理可配合時間`。",
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
    excluded_day_values: set[str] | None = None,
    schedule_summary: str = "",
    response_deferred: bool = False,
    prepared_slots: list[tuple[datetime, datetime]] | None = None,
    finalize_batch: bool = False,
):
    if not response_deferred:
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
        slots = prepared_slots if prepared_slots is not None else time_collection.build_mode_slots(
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
    next_excluded_days = set(excluded_day_values or set()) | set(day_values)
    if finalize_batch:
        if created or duplicates:
            cleanup_text = await _cleanup_notice(artist, interaction)
            await _send_pool_completion_notifications(
                interaction, artist, month, deadline_ts
            )
            _set_drafts(interaction.user.id, month, deadline_ts, [])
            created_text = (
                "✅ 已一次送出以下月度可配合時段：\n"
                + "\n".join(f"・{line}" for line in created)
                if created
                else "✅ 本次時段先前已存在，未重複新增。"
            )
            await interaction.followup.send(
                created_text
                + duplicate_text
                + skipped_text
                + _conflict_text(conflicts)
                + _warning_text(warnings)
                + late_text
                + cleanup_text
                + "\n\n本次回報已完成；正式安排前營運仍會再確認。",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            "⚠️ 最新既定任務已占用本次所選時段，尚未寫入 Notion。請返回重新調整。"
            + skipped_text
            + _conflict_text(conflicts),
            view=PoolContinueView(
                month,
                deadline_ts,
                nature_code,
                week_values,
                _draft_day_values(_drafts_for(interaction.user.id, month, deadline_ts)),
                schedule_summary=schedule_summary,
                draft_count=len(_drafts_for(interaction.user.id, month, deadline_ts)),
            ),
            ephemeral=True,
        )
        return
    if not created:
        await interaction.followup.send(
            "⚠️ 沒有新增新的未來時段。"
            + duplicate_text
            + skipped_text
            + _conflict_text(conflicts)
            + _warning_text(warnings)
                + "\n\n若要修改，請使用 `/管理可配合時間`。",
            view=PoolContinueView(
                month,
                deadline_ts,
                nature_code,
                week_values,
                next_excluded_days,
                schedule_summary=schedule_summary,
            ),
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
        view=PoolContinueView(
            month,
            deadline_ts,
            nature_code,
            week_values,
            next_excluded_days,
            schedule_summary=schedule_summary,
        ),
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
        excluded_day_values: set[str] | None = None,
        schedule_summary: str = "",
        edit_group_index: int | None = None,
    ):
        super().__init__(title="補充可用時間")
        self.month = month
        self.deadline_ts = deadline_ts
        self.nature_code = nature_code
        self.week_values = week_values
        self.day_values = day_values
        self.mode = mode
        self.excluded_day_values = set(excluded_day_values or set())
        self.schedule_summary = schedule_summary
        self.edit_group_index = edit_group_index
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
        view = AvailabilityConfirmView(
            self.month,
            self.deadline_ts,
            self.nature_code,
            self.week_values,
            self.day_values,
            self.mode,
            start_text=self.start_input.value if self.start_input else "",
            end_text=self.end_input.value if self.end_input else "",
            excluded_day_values=self.excluded_day_values,
            schedule_summary=self.schedule_summary,
            edit_group_index=self.edit_group_index,
        )
        await interaction.response.send_message(view.prompt(), view=view, ephemeral=True)


class AvailabilityConfirmView(InteractionDebugView):
    """Confirm one date/time group before adding it to the in-memory draft."""

    def __init__(
        self,
        month: str,
        deadline_ts: int,
        nature_code: str,
        week_values: list[str],
        day_values: list[str],
        mode: str,
        *,
        start_text: str = "",
        end_text: str = "",
        excluded_day_values: set[str] | None = None,
        schedule_summary: str = "",
        edit_group_index: int | None = None,
    ):
        super().__init__()
        self.month = month
        self.deadline_ts = deadline_ts
        self.nature_code = nature_code
        self.week_values = week_values
        self.day_values = day_values
        self.mode = mode
        self.start_text = start_text
        self.end_text = end_text
        self.excluded_day_values = set(excluded_day_values or set())
        self.schedule_summary = schedule_summary
        self.edit_group_index = edit_group_index
        if edit_group_index is not None:
            for child in self.children:
                if getattr(child, "label", None) == "加入本次回報":
                    child.label = "儲存修改"

    def prompt(self) -> str:
        dates = "、".join(
            f"{date.fromisoformat(value).month}/{date.fromisoformat(value).day}"
            for value in self.day_values
        )
        if self.mode == "all_day":
            time_text = "08:00–24:00"
        elif self.mode == "after":
            time_text = f"{self.start_text} 之後"
        elif self.mode == "before":
            time_text = f"{self.end_text} 之前"
        else:
            time_text = f"{self.start_text}–{self.end_text}"
        action_text = (
            f"請確認第 {self.edit_group_index + 1} 組修改內容："
            if self.edit_group_index is not None
            else "請確認要加入本次回報的時段："
        )
        return (
            f"{action_text}\n"
            f"日期：{dates}\n"
            f"時間：{time_text}\n\n"
            "儲存後不會立即寫入 Notion；全部確認後才會整批送出。"
        )

    @discord.ui.button(label="加入本次回報", style=discord.ButtonStyle.success, emoji="➕")
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            group = AvailabilityDraftGroup(
                day_values=tuple(self.day_values),
                mode=self.mode,
                start_text=self.start_text,
                end_text=self.end_text,
            )
            group.slots()
        except (ValueError, timeparse.ParseError) as error:
            await interaction.response.send_message(f"❌ {error}", ephemeral=True)
            return
        groups = _drafts_for(interaction.user.id, self.month, self.deadline_ts)
        if self.edit_group_index is None:
            groups.append(group)
            action_text = f"✅ 已加入本次回報：{len(group.day_values)} 天。"
        else:
            try:
                groups = _replace_draft_group(groups, self.edit_group_index, group)
            except IndexError:
                await interaction.response.send_message(
                    "❌ 這組暫存內容已變更，請返回全部暫存重新選擇。",
                    ephemeral=True,
                )
                return
            action_text = f"✅ 已更新第 {self.edit_group_index + 1} 組：{len(group.day_values)} 天。"
        _set_drafts(interaction.user.id, self.month, self.deadline_ts, groups)
        excluded = set(self.excluded_day_values) | _draft_day_values(groups)
        await interaction.response.edit_message(
            content=(
                f"{action_text}\n"
                f"目前暫存 **{len(groups)} 組、{len(_draft_slots(groups))} 個時段**；"
                "尚未寫入 Notion。"
            ),
            view=PoolContinueView(
                self.month,
                self.deadline_ts,
                self.nature_code,
                self.week_values,
                excluded,
                schedule_summary=self.schedule_summary,
                draft_count=len(groups),
            ),
        )

    @discord.ui.button(label="返回修改", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button):
        groups = _drafts_for(interaction.user.id, self.month, self.deadline_ts)
        view = DatePresetView(
            self.month,
            self.deadline_ts,
            self.week_values,
            self.nature_code,
            selected_day_values=self.day_values,
            selected_mode=self.mode,
            excluded_day_values=self.excluded_day_values,
            schedule_summary=self.schedule_summary,
            draft_count=len(groups),
            edit_group_index=self.edit_group_index,
        )
        await interaction.response.edit_message(content=view.prompt(), view=view)


class AvailabilityConflictConfirmView(AvailabilityConfirmView):
    """Second confirmation shown only when fixed schedules trim availability."""

    def __init__(
        self,
        month: str,
        deadline_ts: int,
        nature_code: str,
        week_values: list[str],
        day_values: list[str],
        mode: str,
        *,
        original_slots: list[tuple[datetime, datetime]],
        adjusted_windows: list[tuple[datetime, datetime]],
        conflicts: list[repositories.ScheduleConflict],
        blocked_slots: list[tuple[datetime, datetime]],
        start_text: str = "",
        end_text: str = "",
        excluded_day_values: set[str] | None = None,
        schedule_summary: str = "",
    ):
        super().__init__(
            month,
            deadline_ts,
            nature_code,
            week_values,
            day_values,
            mode,
            start_text=start_text,
            end_text=end_text,
            excluded_day_values=excluded_day_values,
            schedule_summary=schedule_summary,
        )
        self.original_slots = original_slots
        self.adjusted_windows = adjusted_windows
        self.conflicts = conflicts
        self.blocked_slots = blocked_slots
        for child in self.children:
            if getattr(child, "label", None) in {"確認送出", "確認調整後送出"}:
                child.label = "確認調整後送出"
                child.disabled = not adjusted_windows

    def prompt(self) -> str:
        original_lines = [timeparse.humanize(start, end) for start, end in self.original_slots[:8]]
        adjusted_lines = [timeparse.humanize(start, end) for start, end in self.adjusted_windows[:12]]
        conflict_text = _conflict_text(self.conflicts).replace(
            "⚠️ 已排除與正式安排衝突的時間：",
            "⚠️ 你已有以下既定任務，重疊時間不會寫入：",
        )
        parts = [
            "⚠️ **選擇的時間與既定安排重疊**",
            "",
            "原本選擇：",
            *[f"・{line}" for line in original_lines],
        ]
        if adjusted_lines:
            parts.extend(
                [
                    "",
                    "實際將寫入：",
                    *[f"・{line}" for line in adjusted_lines],
                ]
            )
        else:
            parts.extend(["", "本次所選時段已全部被既定安排占用，不會寫入 Notion。"])
        if self.blocked_slots and adjusted_lines:
            blocked_lines = [timeparse.humanize(start, end) for start, end in self.blocked_slots[:8]]
            parts.extend(
                [
                    "",
                    "完全無法寫入：",
                    *[f"・{line}" for line in blocked_lines],
                ]
            )
        parts.append(conflict_text)
        parts.append(
            "\n確認後仍會重新檢查最新任務；需要調整請按「返回修改」。"
            if adjusted_lines
            else "\n請按「返回修改」重新選擇日期或時段。"
        )
        return "\n".join(parts)

    @discord.ui.button(label="確認調整後送出", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="⏳ 正在重新檢查並寫入可配合時間……", view=None)
        await _submit_pool_slots(
            interaction,
            self.month,
            self.deadline_ts,
            self.nature_code,
            self.week_values,
            self.day_values,
            self.mode,
            start_text=self.start_text,
            end_text=self.end_text,
            excluded_day_values=self.excluded_day_values,
            schedule_summary=self.schedule_summary,
            response_deferred=True,
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
            view = AvailabilityConfirmView(
                self.parent_view.month,
                self.parent_view.deadline_ts,
                self.parent_view.nature_code,
                self.parent_view.week_values,
                self.parent_view.day_values,
                self.mode,
                excluded_day_values=self.parent_view.excluded_day_values,
                schedule_summary=self.parent_view.schedule_summary,
                edit_group_index=self.parent_view.edit_group_index,
            )
            await interaction.response.edit_message(content=view.prompt(), view=view)
            return
        await interaction.response.send_modal(
            ModeTimeModal(
                self.parent_view.month,
                self.parent_view.deadline_ts,
                self.parent_view.nature_code,
                self.parent_view.week_values,
                self.parent_view.day_values,
                self.mode,
                excluded_day_values=self.parent_view.excluded_day_values,
                schedule_summary=self.parent_view.schedule_summary,
                edit_group_index=self.parent_view.edit_group_index,
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
        excluded_day_values: set[str] | None = None,
        schedule_summary: str = "",
        draft_count: int = 0,
        today: date | None = None,
        edit_group_index: int | None = None,
    ):
        super().__init__()
        self.month = month
        self.deadline_ts = deadline_ts
        self.week_values = week_values
        self.nature_code = nature_code
        self.today = today
        self.edit_group_index = edit_group_index
        self.excluded_day_values = (
            set(excluded_day_values or set())
            | _past_day_values(month, today=today)
        )
        excluded_dates = {
            date.fromisoformat(value) for value in self.excluded_day_values
        }
        # 讓月份平均分成兩頁，每頁約 15-16 天；仍低於 Discord 的 25 項限制。
        self.day_pages = time_collection.day_option_pages(
            week_values,
            page_size=time_collection.MONTH_DATE_PAGE_SIZE,
            excluded_dates=excluded_dates,
        ) or [[]]
        self.page_index = max(0, min(page_index, len(self.day_pages) - 1))
        self.current_day_options = self.day_pages[self.page_index]
        self.all_day_values = [
            option.value for page in self.day_pages for option in page
        ]
        self.day_values = self._sort_day_values(selected_day_values or [])
        self.mode = selected_mode
        self.schedule_summary = schedule_summary
        self.draft_count = draft_count
        if edit_group_index is not None:
            for child in self.children:
                if getattr(child, "label", None) == "取消":
                    child.label = "取消修改"
        if self.current_day_options:
            self.add_item(DaySelect(self))
        self.add_item(
            ApplyModeButton(
                self,
                "整天可（08:00–24:00）",
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
        if self.current_day_options:
            page_start = self.current_day_options[0].label.split("（", 1)[0]
            page_end = self.current_day_options[-1].label.split("（", 1)[0]
            page_range = page_start if page_start == page_end else f"{page_start}–{page_end}"
        else:
            page_range = "無剩餘日期"
        selected_labels = {
            option.value: option.label.split("（", 1)[0]
            for page in self.day_pages
            for option in page
        }
        selected_days = "、".join(
            selected_labels[value]
            for value in self.day_values
            if value in selected_labels
        )
        selected_hint = time_collection.availability_page_hint(len(self.day_values), self.mode)
        selected_text = f"已選：{selected_days}" if selected_days else "尚未選日期"
        persistent_context = f"{self.schedule_summary}\n" if self.schedule_summary else ""
        draft_text = (
            f"本次已暫存 **{self.draft_count} 組**；全部選完後再一次送出。\n"
            if self.draft_count
            else ""
        )
        edit_text = (
            f"✏️ 正在修改第 {self.edit_group_index + 1} 組；儲存後會取代原內容。\n"
            if self.edit_group_index is not None
            else ""
        )
        return persistent_context + (
            draft_text
            + edit_text
            + f"請勾選要套用同一時段的日期。\n"
            f"日期頁：**{self.page_index + 1}/{page_count}**｜本頁：{page_range}\n"
            f"{selected_text}（共 {len(self.day_values)} 天）。{selected_hint}\n"
            "不同時段請分組加入；全部選完後再一次送出。\n\n"
            f"{time_collection.availability_selection_sop()}"
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
            excluded_day_values=self.excluded_day_values,
            schedule_summary=self.schedule_summary,
            draft_count=self.draft_count,
            today=self.today,
            edit_group_index=self.edit_group_index,
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

    @discord.ui.button(label="重新選日期", style=discord.ButtonStyle.secondary, emoji="↩️", row=4)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button):
        view = DatePresetView(
            self.month,
            self.deadline_ts,
            self.week_values,
            self.nature_code,
            excluded_day_values=self.excluded_day_values,
            schedule_summary=self.schedule_summary,
            draft_count=self.draft_count,
            today=self.today,
            edit_group_index=self.edit_group_index,
        )
        await interaction.response.edit_message(content=view.prompt(), view=view)

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary, emoji="✖️", row=4)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self.edit_group_index is not None:
            groups = _drafts_for(interaction.user.id, self.month, self.deadline_ts)
            await interaction.response.edit_message(
                content="已取消修改，原本暫存內容保持不變。",
                view=PoolContinueView(
                    self.month,
                    self.deadline_ts,
                    self.nature_code,
                    self.week_values,
                    _draft_day_values(groups),
                    schedule_summary=self.schedule_summary,
                    draft_count=len(groups),
                ),
            )
            return
        _set_drafts(interaction.user.id, self.month, self.deadline_ts, [])
        await interaction.response.edit_message(
            content="已取消本次操作，暫存日期與時間已清除，不會寫入 Notion。",
            view=None,
        )
        self.stop()

class PoolContinueView(InteractionDebugView):
    def __init__(
        self,
        month: str,
        deadline_ts: int,
        nature_code: str,
        week_values: list[str] | None = None,
        excluded_day_values: set[str] | None = None,
        schedule_summary: str = "",
        draft_count: int = 0,
    ):
        super().__init__()
        self.month = month
        self.deadline_ts = deadline_ts
        self.nature_code = nature_code
        self.week_values = week_values
        self.excluded_day_values = set(excluded_day_values or set())
        self.schedule_summary = schedule_summary
        self.draft_count = draft_count

    @discord.ui.button(label="繼續選日期", style=discord.ButtonStyle.primary, emoji="➕")
    async def continue_selecting(self, interaction: discord.Interaction, _: discord.ui.Button):
        groups = _drafts_for(interaction.user.id, self.month, self.deadline_ts)
        excluded = _draft_day_values(groups) | _past_day_values(self.month)
        if self.week_values:
            all_day_values = [
                option.value
                for page in time_collection.day_option_pages(self.week_values)
                for option in page
            ]
            if set(all_day_values) <= excluded:
                await interaction.response.edit_message(
                    content="✅ 這個月份的日期都已加入暫存。請檢查全部時段，或先查看／移除已選項目。",
                    view=PoolContinueView(
                        self.month,
                        self.deadline_ts,
                        self.nature_code,
                        self.week_values,
                        excluded,
                        schedule_summary=self.schedule_summary,
                        draft_count=len(groups),
                    ),
                )
                return
            view = DatePresetView(
                self.month,
                self.deadline_ts,
                self.week_values,
                self.nature_code,
                excluded_day_values=excluded,
                schedule_summary=self.schedule_summary,
                draft_count=len(groups),
            )
            await interaction.response.edit_message(
                content=view.prompt(),
                view=view,
            )
            return
        await interaction.response.edit_message(
            content=f"請繼續選擇 **{self.month}** 下一批日期：",
            view=DatePresetView(
                self.month,
                self.deadline_ts,
                [week.value for week in time_collection.month_week_options(self.month)],
                self.nature_code,
                schedule_summary=self.schedule_summary,
                draft_count=len(groups),
            ),
        )

    @discord.ui.button(label="管理全部已選", style=discord.ButtonStyle.secondary, emoji="🗂️")
    async def manage_drafts(self, interaction: discord.Interaction, _: discord.ui.Button):
        groups = _drafts_for(interaction.user.id, self.month, self.deadline_ts)
        if not groups:
            await interaction.response.send_message("目前沒有暫存時段。", ephemeral=True)
            return
        view = AvailabilityDraftManageView(
            self.month,
            self.deadline_ts,
            self.nature_code,
            self.week_values or [],
            groups,
            self.schedule_summary,
        )
        await interaction.response.edit_message(content=view.prompt(), view=view)

    @discord.ui.button(label="檢查全部時段", style=discord.ButtonStyle.success, emoji="✅")
    async def review_all(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        groups = _drafts_for(interaction.user.id, self.month, self.deadline_ts)
        if not groups:
            await interaction.edit_original_response(content="目前沒有暫存時段。", view=self)
            return
        artist = await repositories.find_artist_by_discord_id(interaction.user.id)
        if artist is None:
            await interaction.edit_original_response(
                content="❌ 找不到你的藝人資料，請聯絡管理者確認 Discord User ID。",
                view=self,
            )
            return
        try:
            slots = _draft_slots(groups)
            context = await _schedule_impact_context(artist)
            review_now = datetime.now(config.TZ)
            adjusted, conflicts, blocked = _preview_availability_impact(
                slots,
                context,
                now=review_now,
            )
            past_slots = [(start, end) for start, end in slots if end <= review_now]
        except (ValueError, timeparse.ParseError) as error:
            await interaction.edit_original_response(content=f"❌ {error}", view=self)
            return
        except Exception:
            logger.exception("檢查整批可配合時間失敗")
            await interaction.edit_original_response(
                content="❌ 讀不到既定任務，暫時不能送出，請稍後再試。",
                view=self,
            )
            return
        view = AvailabilityBatchConfirmView(
            self.month,
            self.deadline_ts,
            self.nature_code,
            self.week_values or [],
            groups,
            schedule_summary=self.schedule_summary,
            original_slots=slots,
            adjusted_windows=adjusted,
            conflicts=conflicts,
            blocked_slots=blocked,
            past_slots=past_slots,
        )
        await interaction.edit_original_response(content=view.prompt(), view=view)

    @discord.ui.button(label="取消本次回報", style=discord.ButtonStyle.danger, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        _set_drafts(interaction.user.id, self.month, self.deadline_ts, [])
        await interaction.response.edit_message(
            content="已取消本次回報；暫存內容已清除，未寫入 Notion。",
            view=None,
        )


def _draft_group_text(group: AvailabilityDraftGroup) -> str:
    slots = group.slots()
    if not slots:
        return "（空白時段）"
    dates = [date.fromisoformat(value) for value in group.day_values]
    date_text = (
        f"{dates[0].month}/{dates[0].day}"
        if len(dates) == 1
        else f"{dates[0].month}/{dates[0].day} 等 {len(dates)} 天"
    )
    first_start, first_end = slots[0]
    if first_end.date() == first_start.date() + timedelta(days=1) and first_end.hour == 0:
        time_text = f"{first_start:%H:%M}–24:00"
    else:
        time_text = f"{first_start:%H:%M}–{first_end:%H:%M}"
    return f"{date_text}｜{time_text}"


class AvailabilityDraftManageView(InteractionDebugView):
    def __init__(
        self,
        month: str,
        deadline_ts: int,
        nature_code: str,
        week_values: list[str],
        groups: list[AvailabilityDraftGroup],
        schedule_summary: str,
        page_index: int = 0,
    ):
        super().__init__()
        self.month = month
        self.deadline_ts = deadline_ts
        self.nature_code = nature_code
        self.week_values = week_values
        self.groups = groups
        self.schedule_summary = schedule_summary
        self.page_count = max(1, (len(groups) + 24) // 25)
        self.page_index = max(0, min(page_index, self.page_count - 1))
        page_start = self.page_index * 25
        page_groups = groups[page_start:page_start + 25]
        options = [
            discord.SelectOption(
                label=f"第 {index + 1} 組｜{_draft_group_text(group)}"[:100],
                value=str(index),
                description=f"共 {len(group.day_values)} 天"[:100],
            )
            for index, group in enumerate(page_groups, start=page_start)
        ]
        self.select = discord.ui.Select(
            placeholder="選擇要修改或移除的暫存時段…",
            min_values=1,
            max_values=len(options),
            options=options,
        )
        self.select.callback = self.selection_changed
        self.add_item(self.select)
        if self.page_count > 1:
            previous = discord.ui.Button(
                label="上一頁",
                style=discord.ButtonStyle.secondary,
                disabled=self.page_index == 0,
                row=2,
            )
            following = discord.ui.Button(
                label="下一頁",
                style=discord.ButtonStyle.secondary,
                disabled=self.page_index >= self.page_count - 1,
                row=2,
            )
            previous.callback = self.previous_page
            following.callback = self.next_page
            self.add_item(previous)
            self.add_item(following)

    def prompt(self) -> str:
        page_start = self.page_index * 25
        page_groups = self.groups[page_start:page_start + 25]
        lines = [
            f"**本次暫存｜{len(self.groups)} 組｜頁面 {self.page_index + 1}/{self.page_count}**"
        ]
        for index, group in enumerate(page_groups, start=page_start):
            lines.append(f"{index + 1}. {_draft_group_text(group)}")
        lines.append(
            "\n單選一組可修改；可多選後移除。所有內容仍未寫入 Notion。"
        )
        return _trim_discord_message("\n".join(lines))

    def page_view(self, page_index: int) -> "AvailabilityDraftManageView":
        return AvailabilityDraftManageView(
            self.month,
            self.deadline_ts,
            self.nature_code,
            self.week_values,
            self.groups,
            self.schedule_summary,
            page_index,
        )

    async def previous_page(self, interaction: discord.Interaction):
        view = self.page_view(self.page_index - 1)
        await interaction.response.edit_message(content=view.prompt(), view=view)

    async def next_page(self, interaction: discord.Interaction):
        view = self.page_view(self.page_index + 1)
        await interaction.response.edit_message(content=view.prompt(), view=view)

    async def selection_changed(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

    @discord.ui.button(label="修改選取", style=discord.ButtonStyle.primary, emoji="✏️")
    async def edit_selected(self, interaction: discord.Interaction, _: discord.ui.Button):
        if len(self.select.values) != 1:
            await interaction.response.send_message(
                "一次只能修改一組，請只選一個暫存時段。",
                ephemeral=True,
            )
            return
        group_index = int(self.select.values[0])
        groups = _drafts_for(interaction.user.id, self.month, self.deadline_ts)
        if group_index < 0 or group_index >= len(groups):
            await interaction.response.send_message(
                "這組暫存內容已變更，請重新開啟管理頁。",
                ephemeral=True,
            )
            return
        group = groups[group_index]
        other_groups = [
            item for index, item in enumerate(groups) if index != group_index
        ]
        view = DatePresetView(
            self.month,
            self.deadline_ts,
            self.week_values,
            self.nature_code,
            selected_day_values=list(group.day_values),
            selected_mode=group.mode,
            excluded_day_values=_draft_day_values(other_groups),
            schedule_summary=self.schedule_summary,
            draft_count=len(groups),
            edit_group_index=group_index,
        )
        await interaction.response.edit_message(content=view.prompt(), view=view)

    @discord.ui.button(label="移除選取", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def remove_selected(self, interaction: discord.Interaction, _: discord.ui.Button):
        selected = {int(value) for value in self.select.values}
        remaining = [group for index, group in enumerate(self.groups) if index not in selected]
        _set_drafts(interaction.user.id, self.month, self.deadline_ts, remaining)
        excluded = _draft_day_values(remaining)
        await interaction.response.edit_message(
            content=f"已移除 {len(selected)} 組；目前暫存 {len(remaining)} 組。",
            view=PoolContinueView(
                self.month,
                self.deadline_ts,
                self.nature_code,
                self.week_values,
                excluded,
                schedule_summary=self.schedule_summary,
                draft_count=len(remaining),
            ),
        )

    @discord.ui.button(label="返回", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(
            content=f"目前暫存 **{len(self.groups)} 組、{len(_draft_slots(self.groups))} 個時段**。",
            view=PoolContinueView(
                self.month,
                self.deadline_ts,
                self.nature_code,
                self.week_values,
                _draft_day_values(self.groups),
                schedule_summary=self.schedule_summary,
                draft_count=len(self.groups),
            ),
        )


class AvailabilityBatchConfirmView(InteractionDebugView):
    def __init__(
        self,
        month: str,
        deadline_ts: int,
        nature_code: str,
        week_values: list[str],
        groups: list[AvailabilityDraftGroup],
        *,
        original_slots: list[tuple[datetime, datetime]],
        adjusted_windows: list[tuple[datetime, datetime]],
        conflicts: list[repositories.ScheduleConflict],
        blocked_slots: list[tuple[datetime, datetime]],
        past_slots: list[tuple[datetime, datetime]] | None = None,
        schedule_summary: str = "",
    ):
        super().__init__()
        self.month = month
        self.deadline_ts = deadline_ts
        self.nature_code = nature_code
        self.week_values = week_values
        self.groups = groups
        self.original_slots = original_slots
        self.adjusted_windows = adjusted_windows
        self.conflicts = conflicts
        self.blocked_slots = blocked_slots
        self.past_slots = list(past_slots or [])
        self.schedule_summary = schedule_summary
        for child in self.children:
            if getattr(child, "label", None) == "全部確認送出":
                child.disabled = not adjusted_windows

    def prompt(self) -> str:
        parts = [
            f"**{self.month} 本次回報總覽**",
            f"共 {len(self.groups)} 組、{len(self.original_slots)} 個原始時段。",
            "",
            "實際將寫入：",
        ]
        if self.adjusted_windows:
            parts.extend(
                f"・{timeparse.humanize(start, end)}"
                for start, end in self.adjusted_windows
            )
        else:
            parts.append("・沒有可寫入時段")
        if self.past_slots:
            parts.extend(
                [
                    "",
                    "⏭️ 已略過過去時段：",
                    *[
                        f"・{timeparse.humanize(start, end)}"
                        for start, end in self.past_slots
                    ],
                ]
            )
        if self.conflicts:
            parts.append(
                _conflict_text(self.conflicts).replace(
                    "⚠️ 已排除與正式安排衝突的時間：",
                    "⚠️ 你已有以下既定任務，重疊時間不會寫入：",
                )
            )
        if self.blocked_slots:
            parts.append(f"\n完全被占用：{len(self.blocked_slots)} 個原始時段。")
        parts.append(
            "\n按「全部確認送出」後才會一次寫入 Notion；需要調整可返回。"
            if self.adjusted_windows
            else "\n全部時段都無法寫入，請返回調整。"
        )
        return _trim_discord_message("\n".join(parts))

    @discord.ui.button(label="全部確認送出", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(
            content="⏳ 正在重新檢查全部時段並寫入 Notion……",
            view=None,
        )
        await _submit_pool_slots(
            interaction,
            self.month,
            self.deadline_ts,
            self.nature_code,
            self.week_values,
            sorted(_draft_day_values(self.groups)),
            "batch",
            schedule_summary=self.schedule_summary,
            response_deferred=True,
            prepared_slots=self.original_slots,
            finalize_batch=True,
        )

    @discord.ui.button(label="返回調整", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(
            content=f"目前暫存 **{len(self.groups)} 組、{len(self.original_slots)} 個時段**。",
            view=PoolContinueView(
                self.month,
                self.deadline_ts,
                self.nature_code,
                self.week_values,
                _draft_day_values(self.groups),
                schedule_summary=self.schedule_summary,
                draft_count=len(self.groups),
            ),
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
        # 先確認 Discord interaction，再查 Notion 的藝人與既定安排，
        # 避免外部查詢較慢時按鈕被判定為未及時回應。
        await interaction.response.defer(ephemeral=True, thinking=True)
        artist = await repositories.find_artist_by_discord_id(interaction.user.id)
        if artist is None:
            await interaction.followup.send(
                "❌ 找不到你的藝人資料，請聯絡管理者確認 Notion 上的 Discord User ID 設定。",
                ephemeral=True,
            )
            return
        try:
            schedule_summary = _artist_schedule_summary(
                self.month,
                await _schedule_impact_context(artist),
            )
        except Exception:
            logger.exception("讀取藝人既定安排摘要失敗")
            schedule_summary = "⚠️ 目前讀不到既定安排，請先照可用時間填寫；送出時 Bot 會再檢查。\n"
        groups = _drafts_for(interaction.user.id, self.month, self.deadline_ts)
        excluded = _draft_day_values(groups)
        week_values = [
            week.value for week in time_collection.month_week_options(self.month)
        ]
        await interaction.followup.send(
            f"回報 **{self.month} 可配合時間**。\n"
            + schedule_summary
            + (
                f"已接續先前暫存的 {len(groups)} 組時段；尚未寫入 Notion。\n"
                if groups
                else ""
            )
            + "勾選日期並加入一組時段；全部選完後再一次送出。\n\n"
            f"{time_collection.availability_selection_sop()}",
            view=DatePresetView(
                self.month,
                self.deadline_ts,
                week_values,
                self.nature_code,
                excluded_day_values=excluded,
                schedule_summary=schedule_summary,
                draft_count=len(groups),
            ),
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


def _management_slot_line(slot: repositories.AvailabilitySlot) -> str:
    if not slot.start or not slot.end:
        return slot.name or "（未命名時段）"
    return time_collection.format_slot_summary(
        slot.start,
        slot.end,
        slot.note,
        include_nature=False,
    )


def _slot_delete_label(slot: repositories.AvailabilitySlot) -> str:
    if slot.start and slot.end:
        return time_collection.format_slot_summary(slot.start, slot.end, slot.note)[:100]
    return (slot.name or "（未命名時段）")[:100]


def _trim_discord_message(text: str, limit: int = 1900) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 18].rstrip() + "\n…清單過長已截斷"


def _month_value(year: int, month: int, offset: int) -> str:
    index = year * 12 + (month - 1) + offset
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _overview_months(
    slots: list[repositories.AvailabilitySlot],
    now: datetime,
) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for slot in slots:
        month = _slot_month_label(slot)
        if month != "未分類":
            counts[month] = counts.get(month, 0) + 1
    months = {
        _month_value(now.year, now.month, offset)
        for offset in range(12)
    }
    months.update(counts)
    return [(month, counts.get(month, 0)) for month in sorted(months)[:25]]


def availability_overview_text(
    slots: list[repositories.AvailabilitySlot],
    cleanup_count: int = 0,
) -> str:
    lines = ["**我的可配合時間**"]
    if not slots:
        lines.append("目前沒有已填寫的未來時段。")
    else:
        current_month = ""
        for slot in sorted(slots, key=_slot_sort_key):
            month = _slot_month_label(slot)
            if month != current_month:
                current_month = month
                lines.append(f"\n__{month}__")
            lines.append(f"・{_slot_line(slot)}")
    if cleanup_count:
        lines.append(f"\n已自動整理 {cleanup_count} 筆重複、重疊或冗餘時段。")
    lines.append("\n請選擇月份，即可查看既定安排、刪除時段或新增／補填日期。")
    return _trim_discord_message("\n".join(lines))


class AvailabilityMonthOverviewView(InteractionDebugView):
    """跨月份總覽入口；選定月份後進入同一套管理流程。"""

    def __init__(
        self,
        cog: "SchedulingArtistCog | None",
        artist: repositories.Artist,
        slots: list[repositories.AvailabilitySlot],
        now: datetime,
    ):
        super().__init__()
        self.cog = cog
        self.artist = artist
        options = [
            discord.SelectOption(
                label=month,
                value=month,
                description=(f"已填 {count} 筆" if count else "尚未填寫")[:100],
            )
            for month, count in _overview_months(slots, now)
        ]
        self.select = discord.ui.Select(
            placeholder="選擇要管理的月份…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.select.callback = self.month_selected
        self.add_item(self.select)

    async def month_selected(self, interaction: discord.Interaction):
        if self.cog is None:
            await interaction.response.send_message("❌ 管理入口尚未初始化。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.edit_month_management(interaction, self.artist, self.select.values[0])

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="已關閉可配合時間管理。", view=None)
        self.stop()


class ManageAvailabilityView(InteractionDebugView):
    """單一入口：刪除既有時段，或直接進入同月份補填流程。"""

    def __init__(
        self,
        month: str,
        deadline_ts: int,
        nature_code: str,
        artist: repositories.Artist,
        slots: list[repositories.AvailabilitySlot],
        schedule_summary: str,
    ):
        super().__init__()
        self.month = month
        self.deadline_ts = deadline_ts
        self.nature_code = nature_code
        self.artist = artist
        self.slot_by_id = {slot.page_id: slot for slot in slots[:25]}
        self.schedule_summary = schedule_summary
        if self.slot_by_id:
            options = [
                discord.SelectOption(
                    label=_slot_delete_label(slot),
                    value=slot.page_id,
                    description="選取後可刪除，再重新填寫"[:100],
                )
                for slot in self.slot_by_id.values()
            ]
            self.select = discord.ui.Select(
                placeholder="選擇要刪除的既有時段（可多選）…",
                min_values=1,
                max_values=min(25, len(options)),
                options=options,
            )
            # 下拉選單本身也會觸發 Discord interaction；先確認選取，
            # 等使用者按「刪除選取時段」才真正 archive Notion 資料。
            self.select.callback = self.selection_changed
            self.add_item(self.select)
            delete_button = discord.ui.Button(
                label="刪除選取時段",
                style=discord.ButtonStyle.danger,
                emoji="🗑️",
            )
            delete_button.callback = self.delete_selected
            self.add_item(delete_button)
        fill_button = discord.ui.Button(
            label="新增／補填日期",
            style=discord.ButtonStyle.success,
            emoji="➕",
        )
        fill_button.callback = self.open_fill_flow
        self.add_item(fill_button)

        cancel_button = discord.ui.Button(
            label="取消",
            style=discord.ButtonStyle.secondary,
            emoji="✖️",
        )
        cancel_button.callback = self.cancel
        self.add_item(cancel_button)

    async def cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="已取消管理可配合時間，既有資料未變更。",
            view=None,
        )
        self.stop()

    async def selection_changed(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

    async def delete_selected(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        page_ids = list(self.select.values)
        labels = [
            _slot_delete_label(self.slot_by_id[page_id])
            for page_id in page_ids
            if page_id in self.slot_by_id
        ]
        try:
            for page_id in page_ids:
                await repositories.archive_slot(page_id)
            await audit.log_event(
                event_name=f"刪除可用時段｜{self.artist.name}",
                action_type="更新",
                operator=self.artist.name,
                discord_user_id=str(interaction.user.id),
                target_db="Availability Slots",
                before=f"{len(page_ids)} 筆",
                after="（已刪除，等待重新填寫）",
            )
        except Exception:
            logger.exception("管理入口刪除時段失敗")
            await interaction.followup.send("❌ 刪除失敗，請稍後再試。", ephemeral=True)
            return
        await interaction.followup.send(
            "🗑️ 已刪除以下時段；請按原訊息的「新增／補填日期」重新填寫：\n"
            + "\n".join(f"・{label}" for label in labels),
            ephemeral=True,
        )

    async def open_fill_flow(self, interaction: discord.Interaction):
        groups = _drafts_for(interaction.user.id, self.month, self.deadline_ts)
        view = DatePresetView(
            self.month,
            self.deadline_ts,
            [week.value for week in time_collection.month_week_options(self.month)],
            self.nature_code,
            excluded_day_values=_draft_day_values(groups),
            schedule_summary=self.schedule_summary,
            draft_count=len(groups),
        )
        await interaction.response.edit_message(content=view.prompt(), view=view)


class SchedulingArtistCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _month_management_payload(
        self,
        artist: repositories.Artist,
        month: str,
        slots: list[repositories.AvailabilitySlot] | None = None,
    ) -> tuple[str, ManageAvailabilityView]:
        try:
            schedule_summary = _artist_schedule_summary(
                month,
                await _schedule_impact_context(artist),
            )
        except Exception:
            logger.exception("讀取藝人既定安排摘要失敗")
            schedule_summary = "⚠️ 目前讀不到既定安排，請稍後再試。\n"
        now = datetime.now(config.TZ)
        if slots is None:
            slots = _future_slots(await repositories.get_slots_for_artist(artist.page_id))
        current_slots = sorted(
            [slot for slot in slots if _slot_month_label(slot) == month],
            key=_slot_sort_key,
        )
        current_text = "目前尚未填寫這個月份的時段。"
        if current_slots:
            current_text = "目前已填時段：\n" + "\n".join(
                f"・{_management_slot_line(slot)}" for slot in current_slots[:25]
            )
        deadline_ts = int((now + timedelta(days=3650)).timestamp())
        content = _trim_discord_message(
            f"管理 **{month} 可配合時間**。\n"
            + schedule_summary
            + current_text
            + "\n\n選擇既有時段可刪除；要增加時間請按「新增／補填日期」。\n\n"
            + time_collection.availability_selection_sop()
        )
        view = ManageAvailabilityView(
            month,
            deadline_ts,
            time_collection.DEFAULT_COLLECTION_NATURE,
            artist,
            current_slots,
            schedule_summary,
        )
        return content, view

    async def edit_month_management(
        self,
        interaction: discord.Interaction,
        artist: repositories.Artist,
        month: str,
    ) -> None:
        try:
            content, view = await self._month_management_payload(artist, month)
        except Exception:
            logger.exception("讀取月份可配合時間失敗")
            await interaction.edit_original_response(
                content="❌ 讀取可配合時間失敗，請稍後再試。",
                view=None,
            )
            return
        await interaction.edit_original_response(content=content, view=view)

    @app_commands.command(name="管理可配合時間", description="查詢、刪除及補填月度可配合時間")
    @app_commands.describe(月份="選填；例如 2026-08，留空可先查看全部未來時段")
    async def refill_time(self, interaction: discord.Interaction, 月份: str | None = None):
        if not await channel_guard.ensure_schedule_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        normalized_month = time_collection.normalize_month(月份) if 月份 else None
        if 月份 and normalized_month is None:
            await interaction.followup.send(
                "❌ 月份格式不正確，請輸入像 `2026-08` 這樣的格式。",
                ephemeral=True,
            )
            return
        artist = await repositories.find_artist_by_discord_id(interaction.user.id)
        if artist is None:
            await interaction.followup.send(
                "❌ 找不到你的藝人資料，請聯絡管理者確認 Notion 設定。",
                ephemeral=True,
            )
            return
        try:
            compaction, cleanup_count = await _cleanup_artist_time_pool(
                artist,
                artist.name,
                str(interaction.user.id),
            )
        except Exception:
            logger.exception("管理入口自動整理時間池失敗")
            await interaction.followup.send(
                "❌ 讀取或整理可配合時間失敗，請稍後再試。",
                ephemeral=True,
            )
            return
        now = datetime.now(config.TZ)
        future_slots = sorted(
            [slot for slot in compaction.visible if slot.end and slot.end > now],
            key=_slot_sort_key,
        )
        if normalized_month is None:
            await interaction.followup.send(
                availability_overview_text(future_slots, cleanup_count),
                view=AvailabilityMonthOverviewView(self, artist, future_slots, now),
                ephemeral=True,
            )
            return
        try:
            content, view = await self._month_management_payload(
                artist,
                normalized_month,
                future_slots,
            )
        except Exception:
            logger.exception("讀取月份可配合時間失敗")
            await interaction.followup.send(
                "❌ 讀取可配合時間失敗，請稍後再試。",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            content,
            view=view,
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(SchedulingArtistCog(bot))
