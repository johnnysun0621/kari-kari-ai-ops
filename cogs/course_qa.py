"""Discord 課程 AI 助教。"""
import asyncio
import logging
import json
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs import channel_guard
from services import course_intent, course_qa, repositories

logger = logging.getLogger(__name__)
_THREAD_STATE_PATH = Path(".state/course_qa_threads.json")
_MEMORY_STATE_PATH = Path(".state/course_qa_memory.json")
_DISCORD_MESSAGE_LIMIT = 1900


def _split_discord_message(content: str, limit: int = _DISCORD_MESSAGE_LIMIT) -> list[str]:
    """切成低於 Discord 2,000 字限制的訊息，優先在換行處切斷。"""
    chunks: list[str] = []
    remaining = content.strip()
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit + 1)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks or [""]


class CourseQACog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._thread_by_user = self._load_thread_state()
        self._memory_by_user = self._load_memory_state()
        self._records_cache: dict[int, list[repositories.CourseMeetingRecord]] = {}
        self._catalog_sent_threads: set[int] = set()
        self._warm_task: asyncio.Task | None = None

    async def cog_load(self):
        """啟動時預熱課程目錄；只讀 Notion，不呼叫 LLM。"""
        self._warm_task = asyncio.create_task(self._warm_course_cache())

    async def _warm_course_cache(self) -> None:
        await self.bot.wait_until_ready()
        try:
            for artist in await repositories.get_active_artists():
                records = await repositories.get_course_meetings_for_artist(artist.page_id)
                if artist.discord_user_id:
                    self._records_cache[int(artist.discord_user_id)] = records
            logger.info("課程助教目錄預熱完成：%d 位藝人", len(self._records_cache))
        except Exception:
            logger.exception("課程助教目錄預熱失敗，改用提問時讀取")

    def cog_unload(self):
        if self._warm_task:
            self._warm_task.cancel()

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
    def _load_memory_state() -> dict[str, dict]:
        try:
            raw = json.loads(_MEMORY_STATE_PATH.read_text(encoding="utf-8"))
            return {str(user_id): value for user_id, value in raw.items() if isinstance(value, dict)}
        except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
            return {}

    def _save_memory_state(self) -> None:
        _MEMORY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _MEMORY_STATE_PATH.write_text(
            json.dumps(self._memory_by_user, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

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
        cached_records = self._records_cache.get(user.id)
        try:
            # Notion 是課程資料的來源；每次提問先同步，避免啟動預熱後看不到新紀錄。
            records = await repositories.get_course_meetings_for_artist(artist.page_id)
            self._records_cache[user.id] = records
        except Exception as exc:
            logger.exception("讀取藝人課程紀錄失敗")
            if cached_records is None:
                return artist, None, f"❌ 讀取課程紀錄失敗，請稍後再試。({type(exc).__name__})"
            logger.warning("改用上次成功快取的課程紀錄：%s", artist.name)
            records = cached_records
        if not records:
            return artist, [], "目前沒有可供你查詢的課程紀錄。請確認控方已勾選 Notion 的「藝人可見」。"
        return artist, records, None

    async def _answer_for_user(self, user: discord.abc.User, question: str) -> str:
        artist, records, error = await self._load_records_for_user(user)
        if error:
            return error
        memory = self._memory_by_user.get(str(user.id), {})
        try:
            intent = await course_intent.classify_query(question, records, memory)
        except Exception:
            logger.exception("課程助教意圖判斷失敗，改用全部課程紀錄")
            intent = course_intent.QueryIntent(mode="compare")
        selected = course_intent.select_records(records, intent, memory)
        intent_context = json.dumps(
            {
                "mode": intent.mode,
                "month": intent.month,
                "terms": intent.terms,
                "attachment_names": intent.attachment_names,
            },
            ensure_ascii=False,
        )
        try:
            answer = await course_qa.answer_course_question(
                artist.name,
                question,
                selected,
                intent_context=intent_context,
                attachment_names=intent.attachment_names,
            )
        except course_qa.CourseQAError as exc:
            logger.warning("課程助教未能回答：%s", exc)
            return exc.user_message
        except Exception:
            logger.exception("課程助教回答階段發生未預期錯誤")
            return "⚠️ 課程助教這次未能完成回答，請稍後重試。"
        self._memory_by_user[str(user.id)] = {
            "last_question": question[:500],
            "mode": intent.mode,
            "month": intent.month,
            "record_ids": [record.page_id for record in selected[:8]],
            "attachment_names": list(intent.attachment_names),
        }
        self._save_memory_state()
        return f"🤖 **課程助教**\n\n{answer}"

    async def _attachment_catalog_for_user(self, user: discord.abc.User) -> str | None:
        artist, records, error = await self._load_records_for_user(user)
        if error:
            return error
        pages = []
        for record in records or []:
            if not record.attachments:
                continue
            files = "、".join(attachment.name for attachment in record.attachments)
            pages.append(f"・[{record.title}]({record.url})\n  檔案：{files}")
        if not pages:
            return None
        return (
            f"📚 **{artist.name} 的課程檔案索引**\n\n"
            + "\n".join(pages)
            + "\n\n請直接說明你要查哪個課程頁或檔案，再自由提問。"
        )

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
        chunks = _split_discord_message(await self._answer_for_user(interaction.user, 問題))
        await interaction.followup.send(chunks[0], ephemeral=True)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """課程助教頻道的自然語言入口；每位藝人使用自己的私人 Thread。"""
        if message.author.bot or not self._is_ai_context(message.channel):
            return
        try:
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
                except (discord.Forbidden, discord.HTTPException):
                    # 沒有管理訊息權限時仍繼續私人回答，但不在公開頻道打擾使用者。
                    logger.warning("課程助教無法刪除公開問題，請補上管理訊息權限")

            if thread.id not in self._catalog_sent_threads:
                self._catalog_sent_threads.add(thread.id)
                catalog = await self._attachment_catalog_for_user(message.author)
                if catalog:
                    await thread.send(catalog)
                    return

            waiting = await thread.send("⏳ 我正在整理你的課程紀錄，請稍候……")
            answer = await self._answer_for_user(message.author, question)
            chunks = _split_discord_message(answer)
            await waiting.edit(content=chunks[0])
            for chunk in chunks[1:]:
                await thread.send(chunk)
        except Exception:
            logger.exception("課程助教訊息處理失敗")
            try:
                await message.channel.send("⚠️ 課程助教處理失敗，請稍後再試；Bot 仍在運作。")
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("課程助教錯誤通知也發送失敗")

    async def cog_app_command_error(self, interaction: discord.Interaction, error):
        logger.exception("課程問答指令失敗", exc_info=error)
        message = "❌ 課程問答執行失敗，請稍後再試。"
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(CourseQACog(bot))
