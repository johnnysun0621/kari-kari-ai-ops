"""Notion 營運布告欄同步到 Discord 公告區。"""
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from services import repositories
from services.permissions import is_admin

logger = logging.getLogger(__name__)
_STATE_PATH = Path(".state/bulletin_sync.json")
_MESSAGE_FORMAT_VERSION = "bulletin-v2"


def bulletin_channel_id() -> int:
    return config.BULLETIN_CHANNEL_ID or config.ANNOUNCE_CHANNEL_ID or config.SCHEDULE_CHANNEL_ID


def bulletin_fingerprint(item: repositories.BulletinItem) -> str:
    raw = "|".join([
        _MESSAGE_FORMAT_VERSION,
        item.title, item.category, item.status, item.date.isoformat() if item.date else "",
        ",".join(item.people), str(item.pinned), item.url,
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def format_bulletin(item: repositories.BulletinItem) -> str:
    """產生不超過 Discord 訊息限制的布告內容。"""
    pin = "📌 " if item.pinned else ""
    date_text = item.date.strftime("%Y/%m/%d %H:%M") if item.date else "未填日期"
    people = "、".join(item.people) if item.people else "未指定"
    lines = [
        f"{pin}**{item.title}**",
        f"類別：{item.category}｜狀態：{item.status}",
        f"日期：{date_text}｜人員：{people}",
    ]
    if item.url:
        lines.append(f"[開啟 Notion 頁面]({item.url})")
    return "\n".join(lines)[:1900]


def _load_state() -> dict[str, dict[str, str]]:
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, dict[str, str]]) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


class BulletinFeedbackModal(discord.ui.Modal, title="回報這則公告"):
    feedback = discord.ui.TextInput(
        label="回饋內容",
        placeholder="請輸入問題、補充或需要營運處理的事項",
        style=discord.TextStyle.paragraph,
        max_length=1800,
        required=True,
    )

    def __init__(self, cog: "BulletinCog", source_page_id: str):
        super().__init__()
        self.cog = cog
        self.source_page_id = source_page_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.cog.save_feedback(
                source_page_id=self.source_page_id,
                user=interaction.user,
                feedback=self.feedback.value,
                message_url="",
            )
        except Exception:
            logger.exception("Discord 布告欄回饋寫入 Notion 失敗")
            await interaction.followup.send("❌ 回饋寫入失敗，請稍後再試。", ephemeral=True)
            return
        await interaction.followup.send("✅ 回饋已寫入 Notion 布告欄，控方可以在那裡處理。", ephemeral=True)


class BulletinFeedbackView(discord.ui.View):
    def __init__(self, cog: "BulletinCog", source_page_id: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.source_page_id = source_page_id
        button = discord.ui.Button(
            label="回報／留言",
            style=discord.ButtonStyle.secondary,
            custom_id=f"bulletin_feedback:{source_page_id}",
        )
        button.callback = self.open_modal
        self.add_item(button)

    async def open_modal(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            BulletinFeedbackModal(self.cog, self.source_page_id)
        )


class BulletinCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._registered_message_ids: set[int] = set()
        self.sync_loop.start()

    def cog_unload(self):
        self.sync_loop.cancel()

    async def save_feedback(
        self,
        source_page_id: str,
        user: discord.abc.User,
        feedback: str,
        message_url: str,
    ) -> None:
        items = await repositories.get_bulletin_items()
        source = next((item for item in items if item.page_id == source_page_id), None)
        if source is None:
            raise ValueError("找不到原始 Notion 布告")
        await repositories.create_bulletin_feedback(
            source=source,
            discord_user=str(user),
            discord_user_id=user.id,
            feedback=feedback,
            discord_message_url=message_url,
        )

    async def _register_feedback_view(self, message: discord.Message, page_id: str) -> None:
        # 每次同步都註冊，讓 Bot 重啟後既有公告上的按鈕仍可使用。
        if message.id in self._registered_message_ids:
            return
        self.bot.add_view(BulletinFeedbackView(self, page_id), message_id=message.id)
        self._registered_message_ids.add(message.id)

    async def sync_bulletins(self) -> tuple[int, int, int]:
        channel = self.bot.get_channel(bulletin_channel_id())
        if channel is None:
            raise RuntimeError("找不到布告欄同步頻道，請設定 BULLETIN_CHANNEL_ID 或 ANNOUNCE_CHANNEL_ID")
        items = await repositories.get_bulletin_items()
        state = _load_state()
        created = updated = skipped = 0
        for item in items:
            fingerprint = bulletin_fingerprint(item)
            record = state.get(item.page_id, {})
            if record.get("fingerprint") == fingerprint:
                message_id = record.get("message_id")
                if message_id:
                    try:
                        message = await channel.fetch_message(int(message_id))
                        await message.edit(view=BulletinFeedbackView(self, item.page_id))
                        await self._register_feedback_view(message, item.page_id)
                    except (discord.NotFound, discord.HTTPException, ValueError):
                        pass
                skipped += 1
                continue
            message = None
            message_id = record.get("message_id")
            if message_id:
                try:
                    message = await channel.fetch_message(int(message_id))
                except (discord.NotFound, discord.HTTPException, ValueError):
                    message = None
            embed = discord.Embed(
                title="📌 營運布告欄",
                description=format_bulletin(item),
                color=discord.Color.green(),
                timestamp=datetime.now(config.get_tenant().tz),
            )
            embed.set_footer(text="Notion 營運布告欄｜可按下方按鈕回報／留言")
            if message is None:
                message = await channel.send(
                    embed=embed,
                    view=BulletinFeedbackView(self, item.page_id),
                )
                created += 1
            else:
                await message.edit(
                    embed=embed,
                    view=BulletinFeedbackView(self, item.page_id),
                )
                updated += 1
            await self._register_feedback_view(message, item.page_id)
            state[item.page_id] = {
                "message_id": str(message.id),
                "fingerprint": fingerprint,
            }
        _save_state(state)
        return created, updated, skipped

    @tasks.loop(minutes=config.BULLETIN_SYNC_INTERVAL_MINUTES)
    async def sync_loop(self):
        try:
            await self.sync_bulletins()
        except Exception:
            logger.exception("Notion 布告欄自動同步失敗")

    @sync_loop.before_loop
    async def before_sync_loop(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="布告欄同步", description="管理端：同步 Notion 布告欄到 Discord 公告區")
    @app_commands.default_permissions(administrator=True)
    @is_admin()
    async def sync_command(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            created, updated, skipped = await self.sync_bulletins()
        except Exception:
            logger.exception("手動同步 Notion 布告欄失敗")
            await interaction.followup.send(
                "❌ 布告欄同步失敗，請確認 Notion integration 已分享該布告欄，" 
                "以及 Discord 公告區權限。",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"✅ 布告欄同步完成：新增 {created}、更新 {updated}、未變更 {skipped}。",
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """使用者直接回覆 Bot 公告時，也將回覆寫入 Notion。"""
        if message.author.bot or message.reference is None:
            return
        reference_id = message.reference.message_id
        if reference_id is None:
            return
        state = _load_state()
        source_page_id = next(
            (
                page_id for page_id, record in state.items()
                if record.get("message_id") == str(reference_id)
            ),
            None,
        )
        if source_page_id is None or not message.content.strip():
            return
        try:
            await self.save_feedback(
                source_page_id=source_page_id,
                user=message.author,
                feedback=message.content,
                message_url=message.jump_url,
            )
            await message.reply("✅ 已將你的回覆寫入 Notion 布告欄。", mention_author=False, delete_after=10)
        except Exception:
            logger.exception("Discord 公告回覆寫入 Notion 失敗")

    async def cog_app_command_error(self, interaction: discord.Interaction, error):
        logger.exception("布告欄指令執行錯誤", exc_info=error)
        message = "❌ 布告欄指令執行失敗，請稍後再試。"
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(BulletinCog(bot))
