"""管理者端：月度時間收集與 Task 版正式安排。"""
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs import channel_guard, paging, ui_text
from cogs.scheduling_artist import make_pool_availability_view
from services import audit, overlap, repositories, scheduling_sla, time_collection, timeparse
from services.permissions import is_admin

logger = logging.getLogger(__name__)

AVAILABILITY_CLEANUP_PREVIEW_LIMIT = 20
CANDIDATE_PAGE_SIZE = 25


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
    limit: int | None = None,
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
                if limit is not None and len(candidates) >= limit:
                    return candidates
    return candidates


def _candidate_date_blocks(candidates: list[overlap.Interval]) -> list[list[str]]:
    """依日期分組候選時段；同一天的標題與時段行放在同一個 block 內，
    讓訊息太長需要截斷時可以整天整天地留，不會切壞單一天的內容。"""
    weekday_names = ("一", "二", "三", "四", "五", "六", "日")
    grouped: dict[date, list[str]] = {}
    for start, end in candidates:
        grouped.setdefault(start.date(), []).append(f"{start:%H:%M}–{end:%H:%M}")

    blocks: list[list[str]] = []
    for candidate_date, times in grouped.items():
        weekday = weekday_names[candidate_date.weekday()]
        block = [f"**{candidate_date.month}/{candidate_date.day}（週{weekday}）**"]
        for index in range(0, len(times), 3):
            block.append("　" + "　".join(times[index:index + 3]))
        blocks.append(block)
    return blocks


def _format_candidate_groups(candidates: list[overlap.Interval]) -> list[str]:
    """依日期分組候選時段，讓 Discord 訊息保持短而容易掃讀。"""
    lines: list[str] = []
    for block in _candidate_date_blocks(candidates):
        lines.extend(block)
    return lines


def _missing_artist_names(
    artists: list[repositories.Artist],
    result: repositories.MonthlyPoolMatchResult,
) -> list[str]:
    name_of = {artist.page_id: artist.name for artist in artists}
    return [name_of[pid] for pid in result.missing_artist_page_ids if pid in name_of]


def _monthly_pool_header_lines(
    month: str,
    duration_minutes: int,
    artists: list[repositories.Artist],
    result: repositories.MonthlyPoolMatchResult,
) -> list[str]:
    """比對結果的開頭摘要（月份、藝人、已填狀況）；建立任務與唯讀查詢共用。"""
    missing = _missing_artist_names(artists, result)
    filled_count = len(artists) - len(missing)
    lines = [
        f"**{month} 時間池比對**｜需要 {repositories.format_duration_hours(duration_minutes)}",
        f"比對藝人：{ '、'.join(artist.name for artist in artists) }",
        f"已填月度時段：{filled_count}/{len(artists)} 位",
    ]
    if missing:
        lines.append(f"⚠️ 未填：{ '、'.join(missing) }")
    return lines


def _date_range_label(block_lines: list[str]) -> str:
    """從一頁的內容抓出日期範圍，例如「9/15 – 9/22」。

    控方要找的是日期不是頁碼，跳頁選單顯示這個才有意義。
    """
    headings = [
        line.strip().strip("*")
        for line in block_lines
        if line.strip().startswith("**")
    ]
    if not headings:
        return ""
    first = headings[0].split("（")[0]
    last = headings[-1].split("（")[0]
    return first if first == last else f"{first} – {last}"


def _overlap_query_pages(
    month: str,
    duration_minutes: int,
    artists: list[repositories.Artist],
    result: repositories.MonthlyPoolMatchResult,
) -> tuple[list[str], list[str]]:
    """/時間交集查詢 的唯讀結果，切成多頁。

    回傳 (每頁文字, 每頁的日期範圍標籤)；標籤給跳頁下拉用。
    """
    header = _monthly_pool_header_lines(month, duration_minutes, artists, result)
    candidates = _duration_candidates(result.windows, duration_minutes)
    date_blocks = _candidate_date_blocks(candidates)
    # 250 字保留給每頁的標題、頁碼與結尾說明。
    budget = ui_text.DISCORD_MESSAGE_LIMIT - len("\n".join(header)) - 250
    block_pages = ui_text.paginate_blocks(date_blocks, budget)

    texts: list[str] = []
    labels: list[str] = []
    for index, block_lines in enumerate(block_pages):
        lines = list(header)
        lines.append("")
        lines.append(
            f"符合 {repositories.format_duration_hours(duration_minutes)} 的候選時段"
            f"（第 {index + 1}/{len(block_pages)} 頁）："
        )
        lines.extend(block_lines)
        lines.append("")
        lines.append(
            f"候選時段共 {len(candidates)} 個、{len(date_blocks)} 天；"
            "用「上一頁／下一頁」或下方的跳頁選單看完全部。"
        )
        lines.append("這裡只列候選，不會建立 Notion Project / Task。")
        texts.append(ui_text.trim_message("\n".join(lines)))
        labels.append(_date_range_label(block_lines))
    return texts, labels


def _monthly_pool_result_text(
    month: str,
    duration_minutes: int,
    artists: list[repositories.Artist],
    result: repositories.MonthlyPoolMatchResult,
) -> str:
    lines = _monthly_pool_header_lines(month, duration_minutes, artists, result)
    missing = _missing_artist_names(artists, result)

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

    # 依日期整天整天地加入，寧可少列幾天也不要在訊息長度上限中間把單一天切壞；
    # 200 字是給截斷提示與結尾說明保留的緩衝空間。
    date_blocks = _candidate_date_blocks(candidates)
    header_len = len("\n".join(lines))
    budget = ui_text.DISCORD_MESSAGE_LIMIT - header_len - 200

    shown_lines: list[str] = []
    shown_dates = 0
    used = 0
    for block in date_blocks:
        block_text = "\n".join(block)
        added = len(block_text) + 1
        if shown_dates and used + added > budget:
            break
        shown_lines.extend(block)
        used += added
        shown_dates += 1

    lines.extend(shown_lines)
    if shown_dates < len(date_blocks):
        lines.append(f"（訊息太長，僅顯示前 {shown_dates}／{len(date_blocks)} 天的候選）")
        lines.append("這份文字只是預覽，不影響候選範圍；請透過下方畫面繼續操作，可用選單分頁瀏覽全部候選。")
    else:
        lines.append(f"候選時段共 {len(candidates)} 個，以下全部列出並可分頁選擇。")
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
    # 兩個區塊共用剩餘長度：筆數上限之外還要看實際字數，否則長備註仍會把預覽切壞。
    # 120 字保留給結尾說明。
    budget = ui_text.DISCORD_MESSAGE_LIMIT - len("\n".join(lines)) - 120
    if compaction.updates:
        update_lines = []
        for update in compaction.updates[:AVAILABILITY_CLEANUP_PREVIEW_LIMIT]:
            slot = slot_by_id.get(update.page_id)
            before = _admin_slot_line(slot) if slot else update.page_id
            after = time_collection.format_slot_summary(
                update.start, update.end, slot.note if slot else ""
            )
            update_lines.append(f"・{before}\n　→ {after}")
        title = "\n**會合併更新**"
        shown, _ = ui_text.fit_items(update_lines, budget - len(title))
        lines.append(title)
        lines.extend(shown)
        budget -= len(title) + len("\n".join(shown)) + 2
        hidden_updates = len(compaction.updates) - len(shown)
        if hidden_updates > 0:
            note = f"・另有 {hidden_updates} 筆合併更新"
            lines.append(note)
            budget -= len(note) + 1
    if compaction.redundant:
        redundant_lines = [
            f"・{_admin_slot_line(slot)}"
            for slot in compaction.redundant[:AVAILABILITY_CLEANUP_PREVIEW_LIMIT]
        ]
        title = "\n**建議封存**"
        shown, _ = ui_text.fit_items(redundant_lines, budget - len(title))
        lines.append(title)
        lines.extend(shown)
        hidden_redundant = len(compaction.redundant) - len(shown)
        if hidden_redundant > 0:
            lines.append(f"・另有 {hidden_redundant} 筆建議封存")
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


@dataclass
class CollectionCancelEntry:
    """一筆可取消的收集＋當下的填寫進度（-1 代表進度讀取失敗）。"""
    collection: time_collection.ActiveCollection
    filled: int
    missing: int

    @property
    def key(self) -> str:
        return f"{self.collection.month}:{self.collection.deadline_ts}"

    @property
    def deadline_text(self) -> str:
        return datetime.fromtimestamp(
            self.collection.deadline_ts, tz=config.TZ
        ).strftime("%m/%d %H:%M")

    @property
    def progress_text(self) -> str:
        if self.filled < 0:
            return "進度讀取失敗"
        total = self.filled + self.missing
        return f"已填 {self.filled}/{total} 位"


class CollectionCancelView(discord.ui.View):
    """/取消時間蒐集：選一筆進行中的收集並停止它的所有後續提醒。"""

    def __init__(self, entries: list[CollectionCancelEntry], operator: discord.abc.User):
        super().__init__(timeout=180)
        self.entries = {entry.key: entry for entry in entries}
        self.operator_id = operator.id
        self.select = discord.ui.Select(
            placeholder="選擇要取消的收集…",
            options=[
                discord.SelectOption(
                    label=f"{entry.collection.month}｜截止 {entry.deadline_text}"[:100],
                    value=entry.key,
                    description=f"{entry.progress_text}"[:100],
                )
                for entry in entries[:25]
            ],
        )
        self.select.callback = self._selection_changed
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.operator_id:
            return True
        await interaction.response.send_message(
            "❌ 這個選單只能由原本執行指令的人操作。", ephemeral=True
        )
        return False

    async def _selection_changed(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

    @discord.ui.button(label="確認取消收集", style=discord.ButtonStyle.danger, emoji="🛑", row=1)
    async def confirm_cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self.select.values:
            await interaction.response.send_message("請先選擇要取消的收集。", ephemeral=True)
            return
        entry = self.entries[self.select.values[0]]
        collection = entry.collection
        await interaction.response.defer(ephemeral=True, thinking=True)

        # 收集登記在兩個地方，兩邊都要移除，否則排課 SLA 還是會在 48／72 小時後發提醒。
        time_collection.deregister_collection(collection.month, collection.deadline_ts)
        sla_removed = scheduling_sla.deregister_collection(
            collection.month, collection.deadline_ts
        )
        await audit.log_event(
            event_name=f"取消月度時段收集｜{collection.month}",
            action_type="更新",
            operator=str(interaction.user),
            discord_user_id=str(interaction.user.id),
            target_db="Availability Slots",
            before=f"進行中｜截止 {entry.deadline_text}｜{entry.progress_text}",
            after="已取消，不再催填",
        )
        sla_note = "" if sla_removed else "\n（排課 SLA 沒有對應紀錄，可能是更早的版本建立的，不影響。）"
        await interaction.followup.send(
            f"🛑 已取消 **{collection.month}** 的時段收集（原截止 {entry.deadline_text}）。\n"
            "・不會再催填未填的藝人\n"
            "・不會發逾期通知\n"
            "・藝人已填的時段**保留不動**，仍可用於排課\n\n"
            "⚠️ 已發出的公告與私訊收不回來，請自行刪除公告或在群組說明。"
            + sla_note,
            ephemeral=True,
        )
        self.stop()

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary, emoji="✖️", row=1)
    async def dismiss(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="已關閉，沒有取消任何收集。", view=None)
        self.stop()


class OverlapQueryResultView(paging.PagedTextView):
    """/時間交集查詢 的結果：唯讀列出共同空檔，需要時才接到既有的建立任務流程。

    翻頁與跳頁沿用 `paging.PagedTextView`；這裡只多加「直接建立任務」與「關閉」。
    """

    def __init__(
        self,
        month: str,
        duration_minutes: int,
        chosen_artists: list[repositories.Artist],
        windows: list[overlap.Interval],
        operator: discord.abc.User,
        available_artists: list[repositories.Artist],
        summary_text: str,
        pages: list[str] | None = None,
        page_labels: list[str] | None = None,
    ):
        super().__init__(
            pages or [summary_text],
            operator.id,
            timeout=600,
            page_labels=page_labels,
        )
        self.month = month
        self.duration_minutes = duration_minutes
        self.chosen_artists = chosen_artists
        self.windows = windows
        self.operator = operator
        self.available_artists = available_artists
        self.summary_text = summary_text

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.operator_id:
            return True
        await interaction.response.send_message("❌ 這個查詢結果只能由原本執行指令的人操作。", ephemeral=True)
        return False

    @discord.ui.button(label="直接建立任務", style=discord.ButtonStyle.primary, emoji="📝")
    async def create_task(self, interaction: discord.Interaction, _: discord.ui.Button):
        # 接到既有的 /從時間池建立任務 後段流程；不修改原指令，只是共用同一組畫面。
        await interaction.response.edit_message(
            content=self.summary_text + "\n\n⬇️ 先勾選要安排的候選時段，再選擇安排類型：",
            view=MonthlyPoolCandidateSelectView(
                self.month, self.duration_minutes, self.chosen_artists,
                _duration_candidates(self.windows, self.duration_minutes), self.operator,
                available_artists=self.available_artists,
                summary_text=self.summary_text,
            ),
        )
        self.stop()

    @discord.ui.button(label="關閉", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button):
        # 留下目前翻到的那一頁，避免關閉後看到的內容跳回第一頁。
        await interaction.response.edit_message(content=self.current_text(), view=None)
        self.stop()


class OverlapQueryArtistSelectView(discord.ui.View):
    """/時間交集查詢 的第二步：選藝人後只做唯讀比對，不寫入 Notion。"""

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
            placeholder="選擇要查詢交集的藝人（可多選）…",
            options=options,
            min_values=1,
            max_values=len(options),
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary, row=1, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="已關閉時間交集查詢。", view=None)
        self.stop()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.operator_id:
            return True
        await interaction.response.send_message("❌ 這個查詢選單只能由原本執行指令的人操作。", ephemeral=True)
        return False

    async def on_select(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        chosen = [self.artists[page_id] for page_id in self.select.values]
        artist_page_ids = [artist.page_id for artist in chosen]
        try:
            # 唯讀查詢：不做時間池整理（不寫 Notion），純粹快速看交集。
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
            logger.exception("時間交集查詢失敗")
            await interaction.followup.send("❌ 交集查詢失敗，請稍後再試或查看 bot log。", ephemeral=True)
            return

        result_text = _monthly_pool_result_text(self.month, self.duration_minutes, chosen, result)
        if result.windows:
            # 唯讀查詢要能看完全部候選，所以用分頁而不是截斷。
            pages, page_labels = _overlap_query_pages(
                self.month, self.duration_minutes, chosen, result
            )
            await interaction.followup.send(
                pages[0],
                view=OverlapQueryResultView(
                    self.month, self.duration_minutes, chosen, result.windows,
                    self.operator, list(self.artists.values()), result_text,
                    pages=pages, page_labels=page_labels,
                ),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(result_text, ephemeral=True)
        # 保留原選單，方便直接換一組藝人再查一次。


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

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary, row=1, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(
            content="已取消時間池比對，尚未建立任何任務。",
            view=None,
        )
        self.stop()

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
                    available_artists=list(self.artists.values()),
                    summary_text=result_text,
                ),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(result_text, ephemeral=True)
        # 保留原選單，避免選錯藝人後只能重新輸入 slash command。


class MonthlyPoolCandidateSelectView(discord.ui.View):
    """時間池比對結果：先勾選候選，再直接選 Notion 任務類型。"""

    def __init__(
        self,
        month: str,
        duration_minutes: int,
        artists: list[repositories.Artist],
        windows: list[overlap.Interval],
        operator: discord.abc.User,
        *,
        available_artists: list[repositories.Artist] | None = None,
        summary_text: str = "",
    ):
        super().__init__(timeout=600)
        self.month = month
        self.duration_minutes = duration_minutes
        self.chosen_artists = artists
        self.windows = windows
        self.operator = operator
        self.available_artists = available_artists or list(artists)
        self.operator_id = operator.id
        self.summary_text = summary_text
        self.page = 0
        self.selected_indices: set[int] = set()
        self.select: discord.ui.Select
        self._build_components()

    @property
    def page_count(self) -> int:
        return max(1, (len(self.windows) + CANDIDATE_PAGE_SIZE - 1) // CANDIDATE_PAGE_SIZE)

    def _build_components(self) -> None:
        options = []
        start = self.page * CANDIDATE_PAGE_SIZE
        end = min(start + CANDIDATE_PAGE_SIZE, len(self.windows))
        for index in range(start, end):
            s, e = self.windows[index]
            options.append(
                discord.SelectOption(
                    label=timeparse.humanize(s, e)[:100],
                    value=str(index),
                    description=f"可排 {int((e - s).total_seconds() // 60)} 分鐘",
                    default=index in self.selected_indices,
                )
            )
        self.select = discord.ui.Select(
            placeholder=f"選擇候選時段（第 {self.page + 1}/{self.page_count} 頁）…",
            options=options,
            min_values=1,
            max_values=min(5, len(options)),
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    def _page_text(self) -> str:
        selected = len(self.selected_indices)
        return (
            f"{self.summary_text}\n\n"
            f"⬇️ 勾選候選時段（第 {self.page + 1}/{self.page_count} 頁）\n"
            f"本頁每次最多選 5 個；目前已選 {selected} 個。\n"
            "選好後可翻頁，最後按「確認候選時段」。"
        )

    def _add_navigation_items(self) -> None:
        self.add_item(self.previous_page)
        self.add_item(self.next_page)
        self.add_item(self.confirm_candidates)
        self.add_item(self.back_to_artists)

    async def _change_page(self, interaction: discord.Interaction, page: int) -> None:
        self.page = max(0, min(page, self.page_count - 1))
        self.clear_items()
        self._build_components()
        self._add_navigation_items()
        await interaction.response.edit_message(content=self._page_text(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.operator_id:
            return True
        await interaction.response.send_message("❌ 這個選單只能由原本執行比對的人操作。", ephemeral=True)
        return False

    async def on_select(self, interaction: discord.Interaction):
        current_page_indices = set(range(
            self.page * CANDIDATE_PAGE_SIZE,
            min((self.page + 1) * CANDIDATE_PAGE_SIZE, len(self.windows)),
        ))
        self.selected_indices.difference_update(current_page_indices)
        self.selected_indices.update(int(value) for value in self.select.values)
        await interaction.response.edit_message(content=self._page_text(), view=self)

    @discord.ui.button(label="上一頁", style=discord.ButtonStyle.secondary, row=1)
    async def previous_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._change_page(interaction, self.page - 1)

    @discord.ui.button(label="下一頁", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._change_page(interaction, self.page + 1)

    @discord.ui.button(label="確認候選時段", style=discord.ButtonStyle.success, row=1)
    async def confirm_candidates(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self.selected_indices:
            await interaction.response.send_message("❌ 請至少選一個候選時段。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        chosen = [self.windows[index] for index in sorted(self.selected_indices)]
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
                available_artists=self.available_artists,
                candidate_windows=self.windows,
            ),
            ephemeral=True,
        )
        self.stop()

    @discord.ui.button(label="返回藝人選擇", style=discord.ButtonStyle.secondary, row=2, emoji="↩️")
    async def back_to_artists(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(
            content="請重新選擇要比對的藝人：",
            view=MonthlyPoolArtistSelectView(
                self.available_artists,
                self.month,
                self.duration_minutes,
                self.operator,
            ),
        )


class CourseTypeView(discord.ui.View):
    """課程安排的第二層選擇，即時讀取 Notion Tasks 的「課程類別」選項。"""

    def __init__(self, month, duration_minutes, artists, windows, operator, task_types, course_types, *, available_artists=None, candidate_windows=None):
        super().__init__(timeout=600)
        self.month = month
        self.duration_minutes = duration_minutes
        self.artists = artists
        self.windows = windows
        self.operator_id = operator.id
        self.operator = operator
        self.task_types = task_types
        self.course_types = course_types
        self.available_artists = available_artists or list(artists)
        self.candidate_windows = candidate_windows or windows
        self.select = discord.ui.Select(
            placeholder="選擇課程類型…",
            options=[discord.SelectOption(label=course_type, value=course_type) for course_type in course_types[:25]],
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    @discord.ui.button(label="返回任務類型", style=discord.ButtonStyle.secondary, row=1, emoji="↩️")
    async def back_to_task_type(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(
            content="請重新選擇 Notion 任務類型：",
            view=TaskTypeView(
                self.month, self.duration_minutes, self.artists, self.windows,
                self.operator, self.task_types,
                available_artists=self.available_artists,
                candidate_windows=self.candidate_windows,
            ),
        )

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
                self.task_types, self.available_artists,
                self.candidate_windows,
            ),
            ephemeral=True,
        )
        # 下游 Project 選單提供返回課程類型，不讓選錯後卡死。


class CourseProjectSelectView(discord.ui.View):
    """課程安排選擇上層 Project，Task 建立後可由營運集中管理。"""

    def __init__(self, month, duration_minutes, artists, windows, operator, course_type, teachers, projects, task_types, available_artists, candidate_windows=None):
        super().__init__(timeout=600)
        self.month = month
        self.duration_minutes = duration_minutes
        self.artists = artists
        self.windows = windows
        self.operator_id = operator.id
        self.course_type = course_type
        self.teachers = teachers
        self.task_types = task_types
        self.operator = operator
        self.available_artists = available_artists
        self.candidate_windows = candidate_windows or windows
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
                self.projects.values(), self.task_types, self.available_artists,
                self.candidate_windows,
            ),
            ephemeral=True,
        )
        # 老師選單可返回本層重新選 Project。

    @discord.ui.button(label="返回課程類型", style=discord.ButtonStyle.secondary, row=1, emoji="↩️")
    async def back_to_course_type(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer()
        try:
            course_types = await repositories.get_course_type_options()
        except Exception:
            logger.exception("讀取 Tasks 課程類別選項失敗")
            await interaction.followup.send(
                "❌ 讀取 Notion Tasks 的課程類別失敗，請確認 Bot 有讀取 Tasks schema 的權限。",
                ephemeral=True,
            )
            return
        if not course_types:
            await interaction.followup.send(
                "❌ Notion Tasks 的「課程類別」目前沒有可用選項。",
                ephemeral=True,
            )
            return
        await interaction.edit_original_response(
            content="請重新選擇具體課程類型：",
            view=CourseTypeView(
                self.month, self.duration_minutes, self.artists, self.windows,
                self.operator, self.task_types, course_types,
                available_artists=self.available_artists,
                candidate_windows=self.candidate_windows,
            ),
        )


class TeacherSelectView(discord.ui.View):
    """從 People.身份類型＝老師的清單選擇確認對象。"""

    def __init__(self, month, duration_minutes, artists, windows, operator, course_type, teachers, project_page_id, projects, task_types, available_artists, candidate_windows=None):
        super().__init__(timeout=600)
        self.month = month
        self.duration_minutes = duration_minutes
        self.artists = artists
        self.windows = windows
        self.operator_id = operator.id
        self.course_type = course_type
        self.teachers = teachers
        self.project_page_id = project_page_id
        self.projects = list(projects)
        self.task_types = task_types
        self.operator = operator
        self.candidate_windows = candidate_windows or windows
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
        # Modal 取消後仍可回到這個選單重新選老師。

    @discord.ui.button(label="返回 Project", style=discord.ButtonStyle.secondary, row=1, emoji="↩️")
    async def back_to_project(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(
            content="請重新選擇課程培訓 Project：",
            view=CourseProjectSelectView(
                self.month, self.duration_minutes, self.artists, self.windows,
                self.operator, self.course_type, self.teachers, self.projects,
                self.task_types, self.available_artists,
                self.candidate_windows,
            ),
        )


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
                task_type=repositories.COURSE_TASK_TYPE,
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
        scheduling_sla.register_task(
            task.page_id,
            self.month,
            created_ts=int(time.time()),
        )
        await audit.log_event(
            event_name=f"建立課程 Task｜{request_name}",
            action_type="建立", operator=str(interaction.user),
            discord_user_id=str(interaction.user.id),
            target_db="Tasks", target_url=task.url,
            after=f"{repositories.COURSE_TASK_TYPE}｜{self.course_type}｜老師 {self.teacher_name}｜{len(teacher_windows)} 個候選｜{names}",
        )
        lines = "\n".join(f"・{timeparse.humanize(s, e)}" for s, e in teacher_windows)
        task_link = f"\n🔗 [開啟 Notion Task，填正式時間]({task.url})" if task.url else ""
        await interaction.followup.send(
            f"✅ 已建立課程 Task「**{request_name}**」。\n"
            f"課程類別：{self.course_type}｜老師：{self.teacher_name}\n"
            f"候選時段：\n{lines}"
            f"{task_link}\n\n"
            "目前不會通知藝人；請自行與老師協調確認時間，再到 Notion 的 Task 頁面填正式開始／結束時間。"
            "確認內容完整後勾選 `Discord 顯示=true`，Bot 會自動通知藝人。課程作業也在同一筆 Task 管理。",
            ephemeral=True,
        )


class TaskTypeView(discord.ui.View):
    """直接從 Notion Tasks schema 選擇正式任務類型。"""

    def __init__(self, month, duration_minutes, artists, windows, operator, task_types, *, available_artists=None, candidate_windows=None):
        super().__init__(timeout=600)
        self.month = month
        self.duration_minutes = duration_minutes
        self.artists = artists
        self.windows = windows
        self.operator_id = operator.id
        self.operator = operator
        self.task_types = task_types
        self.available_artists = available_artists or list(artists)
        self.candidate_windows = candidate_windows or windows
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
        await interaction.response.defer(ephemeral=True, thinking=True)
        task_type = self.select.values[0]
        if task_type == repositories.COURSE_TASK_TYPE:
            try:
                course_types = await repositories.get_course_type_options()
            except Exception:
                logger.exception("讀取 Tasks 課程類別選項失敗")
                await interaction.followup.send(
                    "❌ 讀取 Notion Tasks 的課程類別失敗，請確認 Bot 有讀取 Tasks schema 的權限。",
                    ephemeral=True,
                )
                return
            if not course_types:
                await interaction.followup.send(
                    "❌ Notion Tasks 的「課程類別」目前沒有可用選項。",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                content="請選擇具體課程類型：",
                view=CourseTypeView(
                    self.month, self.duration_minutes, self.artists,
                    self.windows, interaction.user, self.task_types, course_types,
                    available_artists=self.available_artists,
                    candidate_windows=self.candidate_windows,
                ), ephemeral=True,
            )
            return
        await interaction.followup.send(
            content=f"請選擇 **{task_type}** 要定案的候選時段：",
            view=DirectArrangementCandidateView(
                task_type, task_type, self.month, self.duration_minutes,
                self.artists, self.windows, interaction.user,
                self.task_types,
                available_artists=self.available_artists,
                candidate_windows=self.candidate_windows,
            ),
            ephemeral=True,
        )

    @discord.ui.button(label="返回候選時段", style=discord.ButtonStyle.secondary, row=1, emoji="↩️")
    async def back_to_candidates(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(
            content="請重新勾選候選時段：",
            view=MonthlyPoolCandidateSelectView(
                self.month, self.duration_minutes, self.artists, self.candidate_windows,
                self.operator, available_artists=self.available_artists,
                summary_text="已返回候選時段選擇。",
            ),
        )


class DirectArrangementCandidateView(discord.ui.View):
    """非課程分流：由控方選候選窗後建立尚未發布的 Task 草稿。"""

    def __init__(self, arrangement_type, task_type, month, duration_minutes, artists, windows, operator, task_types, *, available_artists=None, candidate_windows=None):
        super().__init__(timeout=600)
        self.arrangement_type = arrangement_type
        self.task_type = task_type
        self.month = month
        self.duration_minutes = duration_minutes
        self.artists = artists
        self.windows = windows
        self.operator_id = operator.id
        self.operator = operator
        self.task_types = task_types
        self.available_artists = available_artists or list(artists)
        self.candidate_windows = candidate_windows or windows
        self.page = 0
        self._build_components()

    @property
    def page_count(self) -> int:
        return max(1, (len(self.windows) + CANDIDATE_PAGE_SIZE - 1) // CANDIDATE_PAGE_SIZE)

    def _build_components(self) -> None:
        start = self.page * CANDIDATE_PAGE_SIZE
        page_windows = self.windows[start:start + CANDIDATE_PAGE_SIZE]
        self.select = discord.ui.Select(
            placeholder=f"選擇候選時段（第 {self.page + 1}/{self.page_count} 頁）…",
            options=[
                discord.SelectOption(label=timeparse.humanize(s, e)[:100], value=str(start + i))
                for i, (s, e) in enumerate(page_windows)
            ],
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    def _add_navigation_items(self) -> None:
        self.add_item(self.previous_page)
        self.add_item(self.next_page)
        self.add_item(self.back_to_task_type)

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
        # Modal 可取消；保留候選選單讓營運可改選。

    @discord.ui.button(label="上一頁", style=discord.ButtonStyle.secondary, row=1)
    async def previous_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self.clear_items()
        self._build_components()
        self._add_navigation_items()
        await interaction.response.edit_message(
            content=f"請選擇候選時段（第 {self.page + 1}/{self.page_count} 頁）：",
            view=self,
        )

    @discord.ui.button(label="返回任務類型", style=discord.ButtonStyle.secondary, row=2, emoji="↩️")
    async def back_to_task_type(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(
            content="請重新選擇 Notion 任務類型：",
            view=TaskTypeView(
                self.month, self.duration_minutes, self.artists, self.windows,
                self.operator, self.task_types,
                available_artists=self.available_artists,
                candidate_windows=self.candidate_windows,
            ),
        )

    @discord.ui.button(label="下一頁", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.page = min(self.page_count - 1, self.page + 1)
        self.clear_items()
        self._build_components()
        self._add_navigation_items()
        await interaction.response.edit_message(
            content=f"請選擇候選時段（第 {self.page + 1}/{self.page_count} 頁）：",
            view=self,
        )


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
            "此為控方草稿；補齊內容後勾選 Discord 顯示=true，Bot 會自動通知藝人。"
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
        task_link = f"🔗 [開啟 Notion Task，補齊內容]({task.url})\n" if task.url else ""
        await interaction.followup.send(
            f"✅ 已建立 **{self.arrangement_type}** Task 草稿，尚未通知藝人。\n"
            f"**{task.name}**｜{timeparse.humanize(start, end)}\n"
            f"任務類型：{self.task_type}\n"
            f"{task_link}"
            "請先在 Notion 補齊任務內容；確認後勾選 `Discord 顯示=true`，Bot 會自動通知藝人。",
            ephemeral=True,
        )


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
        if not await channel_guard.ensure_admin_channel(interaction):
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

    # ---------- /取消時間蒐集 ----------
    @app_commands.command(name="取消時間蒐集", description="停止進行中的月度時段收集，不再催填藝人")
    @app_commands.default_permissions(administrator=True)
    @is_admin()
    async def cancel_time_collection(self, interaction: discord.Interaction):
        if not await channel_guard.ensure_admin_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        collections = time_collection.active_collections()
        if not collections:
            await interaction.followup.send(
                "目前沒有進行中的時段收集。\n"
                "（收集全員填完或逾期通知發出後會自動結束，不需要手動取消。）",
                ephemeral=True,
            )
            return

        entries: list[CollectionCancelEntry] = []
        for collection in collections:
            try:
                progress = await repositories.get_monthly_pool_progress(
                    collection.month, list(collection.artist_page_ids)
                )
                filled, missing = (
                    len(progress.filled_artist_page_ids),
                    len(progress.missing_artist_page_ids),
                )
            except Exception:
                # 讀不到進度不該擋住取消——那正是控方最需要這支指令的時候。
                logger.exception("讀取月度時間池進度失敗：%s", collection.month)
                filled, missing = -1, -1
            entries.append(CollectionCancelEntry(collection, filled, missing))

        await interaction.followup.send(
            "選擇要取消的收集。取消後**不會再催填藝人**，也不會發逾期通知。\n"
            "已經發出的公告與私訊收不回來，請自行刪除公告或在群組說明。",
            view=CollectionCancelView(entries, interaction.user),
            ephemeral=True,
        )

    # ---------- /時間交集查詢 ----------
    @app_commands.command(name="時間交集查詢", description="只查看藝人共同空檔，不建立任何任務")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        月份="要查詢的月份，例：2026-08",
        時長分鐘="需要連續可安排的時長，例：60、90、240",
    )
    @is_admin()
    async def query_availability_overlap(
        self,
        interaction: discord.Interaction,
        月份: str,
        時長分鐘: app_commands.Range[int, 15, 600],
    ):
        if not await channel_guard.ensure_admin_channel(interaction):
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
                "❌ Artists 裡沒有可查詢的藝人，請先到 Notion 補 Discord User ID。",
                ephemeral=True,
            )
            return
        limit_note = ""
        if len(artists) > 25:
            limit_note = "\n⚠️ Discord 單次選單最多顯示 25 位，這次先列前 25 位可排課藝人。"
        await interaction.followup.send(
            (
                f"查詢 **{month}** 的共同空檔（唯讀，不會建立任何任務）。\n"
                f"需要連續時長：{repositories.format_duration_hours(時長分鐘)}"
                f"{limit_note}"
            ),
            view=OverlapQueryArtistSelectView(
                artists[:25], month, 時長分鐘, interaction.user,
            ),
            ephemeral=True,
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
        if not await channel_guard.ensure_admin_channel(interaction):
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
