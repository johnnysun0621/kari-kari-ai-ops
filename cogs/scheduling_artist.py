"""藝人端：填寫可用時間按鈕（persistent）、時段 Modal、/我的時段。"""
import logging
import re
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

import config
from services import audit, repositories, timeparse

logger = logging.getLogger(__name__)


async def _verify_participant(
    user: discord.abc.User, request_page_id: str
) -> tuple[repositories.Artist | None, repositories.ScheduleRequest | None, str | None]:
    """回傳 (artist, request, 錯誤訊息)。錯誤訊息為 None 表示驗證通過。"""
    artist = await repositories.find_artist_by_discord_id(user.id)
    if artist is None:
        return None, None, "找不到你的藝人資料，請聯絡管理者確認 Notion 上的 Discord User ID 設定。"
    request = await repositories.get_request(request_page_id)
    if request is None:
        return artist, None, "這筆排課需求已不存在，請聯絡管理者。"
    if artist.page_id not in request.artist_page_ids:
        return artist, request, "你不在這次排課的參與名單中。"
    if request.status not in ("等待藝人填寫", "比對中"):
        return artist, request, f"這筆排課目前狀態是「{request.status}」，已不開放填寫。"
    return artist, request, None


class AvailabilityModal(discord.ui.Modal):
    """單一時段填寫表單，一次一筆，送出後可按「再填一筆」。"""

    def __init__(self, request_page_id: str, request_name: str = ""):
        super().__init__(title=f"填寫可用時間{('｜' + request_name)[:30] if request_name else ''}")
        self.request_page_id = request_page_id
        self.date_input = discord.ui.TextInput(
            label="日期（例：7/15 或 2026-07-15）", placeholder="7/15", max_length=12
        )
        self.start_input = discord.ui.TextInput(
            label="開始時間（例：14:00）", placeholder="14:00", max_length=8
        )
        self.end_input = discord.ui.TextInput(
            label="結束時間（例：18:00，可跨日）", placeholder="18:00", max_length=8
        )
        self.note_input = discord.ui.TextInput(
            label="備註（選填）", required=False, max_length=100
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
                f"❌ {e}\n請再按一次「填寫可用時間」重新輸入。", ephemeral=True
            )
            return

        artist, request, err = await _verify_participant(interaction.user, self.request_page_id)
        if err:
            await interaction.followup.send(f"❌ {err}", ephemeral=True)
            return

        is_late = bool(request.fill_deadline and datetime.now(config.TZ) > request.fill_deadline)
        try:
            slot = await repositories.create_availability_slot(
                self.request_page_id, artist, start, end,
                is_late=is_late, note=self.note_input.value or "",
            )
        except Exception:
            logger.exception("建立可用時段失敗")
            await interaction.followup.send("❌ 寫入 Notion 失敗，請稍後再試或聯絡管理者。", ephemeral=True)
            await audit.log_event(
                event_name=f"填寫可用時間失敗｜{artist.name}",
                action_type="錯誤", operator=artist.name,
                discord_user_id=str(interaction.user.id),
                target_db="Availability Slots", result="失敗",
                error_message="create_availability_slot exception（詳見 bot log）",
            )
            return

        await audit.log_event(
            event_name=f"填寫可用時間｜{artist.name}｜{request.name}",
            action_type="提交", operator=artist.name,
            discord_user_id=str(interaction.user.id),
            target_db="Availability Slots",
            after=timeparse.humanize(start, end),
        )
        late_note = "\n⚠️ 已超過填寫截止時間，此筆標記為逾期提交。" if is_late else ""
        await interaction.followup.send(
            f"✅ 已記錄你的可用時間：**{timeparse.humanize(start, end)}**{late_note}",
            view=AfterSubmitView(self.request_page_id, request.name),
            ephemeral=True,
        )


class AfterSubmitView(discord.ui.View):
    """送出一筆後的後續動作：再填一筆／完成。"""

    def __init__(self, request_page_id: str, request_name: str):
        super().__init__(timeout=600)
        self.request_page_id = request_page_id
        self.request_name = request_name

    @discord.ui.button(label="再填一筆", style=discord.ButtonStyle.primary, emoji="➕")
    async def add_more(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(
            AvailabilityModal(self.request_page_id, self.request_name)
        )

    @discord.ui.button(label="完成", style=discord.ButtonStyle.secondary, emoji="✅")
    async def done(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(
            content="✅ 感謝填寫！所有時段都已記錄，排課結果會再通知你。", view=None
        )


class FillAvailabilityButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"fill_avail:(?P<request_id>[0-9a-f\-]+)",
):
    """公告訊息上的「填寫可用時間」按鈕；custom_id 帶排課需求 page id，bot 重啟後仍有效。"""

    def __init__(self, request_page_id: str):
        self.request_page_id = request_page_id
        super().__init__(
            discord.ui.Button(
                label="📅 填寫可用時間",
                style=discord.ButtonStyle.success,
                custom_id=f"fill_avail:{request_page_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(match["request_id"])

    async def callback(self, interaction: discord.Interaction):
        # Modal 必須是元件互動的第一個回應，Notion 驗證移到 Modal 送出時做
        await interaction.response.send_modal(AvailabilityModal(self.request_page_id))


def make_announcement_view(request_page_id: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(FillAvailabilityButton(request_page_id))
    return view


class MySlotsDeleteView(discord.ui.View):
    """列出自己已填時段，可選一筆刪除（Notion archive）。"""

    def __init__(self, slots: list[repositories.AvailabilitySlot], artist: repositories.Artist):
        super().__init__(timeout=600)
        self.artist = artist
        options = [
            discord.SelectOption(
                label=slot.name[:100] or "（未命名時段）",
                value=slot.page_id,
                description=(slot.note or "")[:100] or None,
            )
            for slot in slots[:25]
        ]
        self.select = discord.ui.Select(placeholder="選擇要刪除的時段…", options=options)
        self.select.callback = self.on_delete
        self.add_item(self.select)

    async def on_delete(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        page_id = self.select.values[0]
        label = next((o.label for o in self.select.options if o.value == page_id), page_id)
        try:
            await repositories.archive_slot(page_id)
        except Exception:
            logger.exception("刪除時段失敗")
            await interaction.followup.send("❌ 刪除失敗，請稍後再試。", ephemeral=True)
            return
        await audit.log_event(
            event_name=f"刪除可用時段｜{self.artist.name}",
            action_type="更新", operator=self.artist.name,
            discord_user_id=str(interaction.user.id),
            target_db="Availability Slots", before=label, after="（已刪除）",
        )
        await interaction.followup.send(f"🗑️ 已刪除：{label}\n可再按公告上的按鈕重新填寫。", ephemeral=True)


class SchedulingArtistCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="我的時段", description="查看並管理我已填寫的可用時間")
    async def my_slots(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        artist = await repositories.find_artist_by_discord_id(interaction.user.id)
        if artist is None:
            await interaction.followup.send(
                "❌ 找不到你的藝人資料，請聯絡管理者確認 Notion 設定。", ephemeral=True
            )
            return
        open_requests = await repositories.get_requests_by_status(["等待藝人填寫", "比對中"])
        my_slots: list[repositories.AvailabilitySlot] = []
        lines: list[str] = []
        for req in open_requests:
            if artist.page_id not in req.artist_page_ids:
                continue
            slots = await repositories.get_slots_for_request(req.page_id)
            mine = [s for s in slots if artist.page_id in s.artist_page_ids]
            if mine:
                lines.append(f"**{req.name}**")
                for s in mine:
                    if s.start and s.end:
                        lines.append(f"・{timeparse.humanize(s.start, s.end)}")
                my_slots.extend(mine)
        if not my_slots:
            await interaction.followup.send("目前沒有已填寫的可用時段。", ephemeral=True)
            return
        await interaction.followup.send(
            "你目前填寫的可用時段：\n" + "\n".join(lines),
            view=MySlotsDeleteView(my_slots, artist),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(SchedulingArtistCog(bot))
