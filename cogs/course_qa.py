"""Discord 課程 AI 助教。"""
import logging
import json
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs import channel_guard
from services import course_qa, repositories

logger = logging.getLogger(__name__)
_THREAD_STATE_PATH = Path(".state/course_qa_threads.json")
_SELECTION_STATE_PATH = Path(".state/course_qa_selections.json")


class CourseSelectionView(discord.ui.View):
    """讓藝人在私人 Thread 先選定一堂課。"""

    def __init__(self, cog: "CourseQACog", user_id: int, records: list[repositories.CourseMeetingRecord]):
        super().__init__(timeout=900)
        self.cog = cog
        self.user_id = user_id
        options = []
        for record in records[:25]:
            date_text = record.date.strftime("%m/%d") if record.date else "未填日期"
            description = (record.summary or "可針對這堂課自由提問").replace("\n", " ")[:100]
            options.append(
                discord.SelectOption(
                    label=f"{date_text}｜{record.title}"[:100],
                    description=description,
                    value=record.page_id,
                )
            )
        select = discord.ui.Select(
            placeholder="先選擇一堂課…",
            min_values=1,
            max_values=1,
            options=options,
        )
        select.callback = self._select_callback
        self.add_item(select)

    async def _select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("這不是你的課程選單。", ephemeral=True)
            return
        select = self.children[0]
        record_id = select.values[0]
        self.cog._selected_record_by_user[str(self.user_id)] = record_id
        self.cog._save_selection_state()
        await interaction.response.edit_message(
            content=(
                f"✅ 已選擇 **{self.cog._record_label(record_id)}**。\n"
                "現在可以直接在這個 Thread 問這堂課的內容；輸入「換課程」可重新選擇。"
            ),
            view=None,
        )


class CourseQACog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._thread_by_user = self._load_thread_state()
        self._selected_record_by_user = self._load_selection_state()
        self._record_labels: dict[str, str] = {}

    @staticmethod
    def _load_thread_state() -> dict[str, int]:
        try:
            raw = json.loads(_THREAD_STATE_PATH.read_text(encoding="utf-8"))
            return {str(user_id): int(thread_id) for user_id, thread_id in raw.items()}
        except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
            return {}

    def _save_thread_state(self) -> None:
        _THREAD_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _THREAD_STATE_PATH.write_text(
            json.dumps(self._thread_by_user, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _load_selection_state() -> dict[str, str]:
        try:
            raw = json.loads(_SELECTION_STATE_PATH.read_text(encoding="utf-8"))
            return {str(user_id): str(page_id) for user_id, page_id in raw.items()}
        except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
            return {}

    def _save_selection_state(self) -> None:
        _SELECTION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SELECTION_STATE_PATH.write_text(
            json.dumps(self._selected_record_by_user, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _record_label(self, record_id: str) -> str:
        return self._record_labels.get(record_id, "這堂課")

    @staticmethod
    def _is_ai_context(channel: discord.abc.GuildChannel | discord.Thread) -> bool:
        if channel_guard.is_ai_channel(getattr(channel, "id", None)):
            return True
        return isinstance(channel, discord.Thread) and channel.parent_id == config.AI_CHANNEL_ID

    async def _load_records_for_user(
        self, user: discord.abc.User
    ) -> tuple[repositories.Artist | None, list[repositories.CourseMeetingRecord] | None, str | None]:
        artist = await repositories.find_artist_by_discord_id(user.id)
        if artist is None:
            return None, None, "❌ 找不到你的藝人資料，請聯絡控方。"
        try:
            records = await repositories.get_course_meetings_for_artist(artist.page_id)
        except Exception as exc:
            logger.exception("讀取藝人課程紀錄失敗")
            return artist, None, f"❌ 讀取課程紀錄失敗，請稍後再試。({type(exc).__name__})"
        if not records:
            return artist, [], "目前沒有可供你查詢的課程紀錄。請確認控方已勾選 Notion 的「藝人可見」。"
        self._record_labels = {record.page_id: record.title for record in records}
        return artist, records, None

    async def _send_course_picker(self, thread: discord.Thread, user: discord.abc.User) -> bool:
        artist, records, error = await self._load_records_for_user(user)
        if error:
            await thread.send(error)
            return False
        if not records:
            return False
        await thread.send(
            f"📚 {artist.name}，請先選擇你要查詢的課程：\n"
            "選定後，你可以針對該堂課自由提問。",
            view=CourseSelectionView(self, user.id, records),
        )
        return True

    async def _answer_for_user(self, user: discord.abc.User, question: str) -> str:
        artist, records, error = await self._load_records_for_user(user)
        if error:
            return error
        selected_id = self._selected_record_by_user.get(str(user.id))
        selected = next((record for record in records if record.page_id == selected_id), None)
        if selected is None:
            return "請先在上方選單選擇一堂課，再開始提問。"
        try:
            answer = await course_qa.answer_course_question(artist.name, question, [selected])
        except course_qa.CourseQAError as exc:
            logger.warning("課程助教未能回答：%s", exc)
            return "⚠️ 課程助教目前尚未完成 API 設定，請聯絡控方。"
        return f"🤖 **課程助教**\n\n{answer}"

    async def _get_or_create_thread(self, message: discord.Message) -> discord.Thread | None:
        thread_id = self._thread_by_user.get(str(message.author.id))
        if thread_id:
            thread = self.bot.get_channel(thread_id)
            if isinstance(thread, discord.Thread) and not thread.archived:
                return thread

        parent = message.channel
        if not isinstance(parent, discord.TextChannel):
            return None
        try:
            thread = await parent.create_thread(
                name=f"課程助教｜{message.author.display_name}"[:100],
                type=discord.ChannelType.private_thread,
                auto_archive_duration=1440,
                invitable=False,
                reason="課程助教私人對話",
            )
            await thread.add_user(message.author)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("建立課程助教私人 Thread 失敗")
            return None
        self._thread_by_user[str(message.author.id)] = thread.id
        self._save_thread_state()
        return thread

    @app_commands.command(name="課程問答", description="查詢自己已確認且可見的課程會議紀錄")
    @app_commands.describe(問題="例如：上次歌唱課老師交代我練習什麼？")
    async def course_question(self, interaction: discord.Interaction, 問題: str):
        if not await channel_guard.ensure_ai_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(
            await self._answer_for_user(interaction.user, 問題), ephemeral=True
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """課程助教頻道的自然語言入口；每位藝人使用自己的私人 Thread。"""
        if message.author.bot or not self._is_ai_context(message.channel):
            return
        question = message.content.strip()
        if not question or question.startswith("/"):
            return

        # Thread 內只接受該 Thread 對應的藝人，避免讀到別人的課程資料。
        if isinstance(message.channel, discord.Thread):
            owner_id = next(
                (user_id for user_id, thread_id in self._thread_by_user.items()
                 if thread_id == message.channel.id),
                None,
            )
            if owner_id != str(message.author.id):
                return
            thread = message.channel
            if question in {"換課程", "換課", "重新選課"}:
                self._selected_record_by_user.pop(str(message.author.id), None)
                self._save_selection_state()
                await self._send_course_picker(thread, message.author)
                return
        else:
            thread = await self._get_or_create_thread(message)
            if thread is None:
                await message.reply(
                    "❌ 無法建立私人課程助教對話，請確認 Bot 有「建立私人 Thread」與「管理訊息」權限。",
                    mention_author=False,
                )
                return
            try:
                await message.delete()
            except discord.Forbidden:
                logger.warning("課程助教無法刪除公開問題，請補上管理訊息權限")
                await thread.send("⚠️ 請補上 Bot 的「管理訊息」權限，避免問題留在公開頻道。")

            # 第一層先選課，不把第一句話直接送給跨課程問答。
            await self._send_course_picker(thread, message.author)
            return

        waiting = await thread.send("⏳ 我正在整理你的課程紀錄，請稍候……")
        answer = await self._answer_for_user(message.author, question)
        await waiting.edit(content=answer)

    async def cog_app_command_error(self, interaction: discord.Interaction, error):
        logger.exception("課程問答指令失敗", exc_info=error)
        message = "❌ 課程問答執行失敗，請稍後再試。"
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(CourseQACog(bot))
