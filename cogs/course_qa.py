"""Discord 課程 AI 助教。"""
import asyncio
import logging
import json
import re
from pathlib import Path

import discord
from discord.ext import commands

import config
from cogs import channel_guard
from services import course_intent, course_qa, repositories

logger = logging.getLogger(__name__)
_THREAD_STATE_PATH = Path(".state/course_qa_threads.json")
_MEMORY_STATE_PATH = Path(".state/course_qa_memory.json")
_DISCORD_MESSAGE_LIMIT = 1900


def can_artist_use_ai(artist: repositories.Artist) -> bool:
    return artist.status not in repositories.INACTIVE_ARTIST_STATUSES


def _sanitize_memory(value: dict) -> dict:
    """只保留檢索狀態，不持久化使用者問題文字。"""
    allowed = {"mode", "month", "record_ids", "attachment_names"}
    return {key: item for key, item in value.items() if key in allowed}


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


class AIAnswerFeedbackModal(discord.ui.Modal, title="回報課程助教回答"):
    feedback = discord.ui.TextInput(
        label="問題說明",
        placeholder="例如：來源連結錯誤、回答漏掉某堂課，或內容不正確",
        style=discord.TextStyle.paragraph,
        max_length=1800,
        required=True,
    )

    def __init__(
        self,
        artist_name: str,
        expected_user_id: int,
        answer_message_url: str,
    ):
        super().__init__()
        self.artist_name = artist_name
        self.expected_user_id = expected_user_id
        self.answer_message_url = answer_message_url

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.expected_user_id:
            await interaction.response.send_message("❌ 這不是你的回答。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await repositories.create_ai_feedback(
                artist_name=self.artist_name,
                discord_user=str(interaction.user),
                discord_user_id=interaction.user.id,
                feedback=self.feedback.value,
                answer_message_url=self.answer_message_url,
            )
        except Exception:
            logger.exception("課程助教問題回報寫入 Notion 失敗")
            await interaction.followup.send("❌ 回報寫入失敗，請稍後再試。", ephemeral=True)
            return
        await interaction.followup.send(
            "✅ 已寫入 Notion 布告欄的「🙋 使用回饋」，控方會在那裡追蹤。",
            ephemeral=True,
        )


class AIAnswerFeedbackView(discord.ui.View):
    def __init__(self, artist_name: str, expected_user_id: int, answer_message_url: str = ""):
        super().__init__(timeout=86400)
        self.artist_name = artist_name
        self.expected_user_id = expected_user_id
        self.answer_message_url = answer_message_url

    @discord.ui.button(label="回報問題", style=discord.ButtonStyle.secondary)
    async def report(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.expected_user_id:
            await interaction.response.send_message("❌ 這不是你的回答。", ephemeral=True)
            return
        await interaction.response.send_modal(AIAnswerFeedbackModal(
            self.artist_name,
            self.expected_user_id,
            self.answer_message_url,
        ))


class CourseQACog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._thread_by_user = self._load_thread_state()
        self._memory_by_user = self._load_memory_state()
        self._save_memory_state()
        self._records_cache: dict[int, list[repositories.CourseMeetingRecord]] = {}
        self._catalog_sent_threads: set[int] = set()
        self._answer_locks: dict[int, asyncio.Lock] = {}
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
            return {
                str(user_id): _sanitize_memory(value)
                for user_id, value in raw.items()
                if isinstance(value, dict)
            }
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
        if not can_artist_use_ai(artist):
            return artist, None, "❌ 目前的合約狀態無法使用課程助教，請聯絡控方。"
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
        """同一位使用者同時只跑一個回答，避免重複消耗 API 額度。"""
        lock = self._answer_locks.setdefault(user.id, asyncio.Lock())
        async with lock:
            return await self._answer_for_user_unlocked(user, question)

    async def _answer_for_user_unlocked(self, user: discord.abc.User, question: str) -> str:
        artist, records, error = await self._load_records_for_user(user)
        if error:
            return error
        memory = self._memory_by_user.get(str(user.id), {})
        try:
            intent = await course_intent.classify_query(
                question,
                records,
                memory,
                usage_key=artist.page_id,
            )
        except Exception:
            logger.exception("課程助教意圖判斷失敗，改用保守的本地檢索")
            intent = course_intent._fallback_intent(question, records)
        selected = course_intent.select_records(records, intent, memory)
        if not selected:
            return "請指定月份、課程名稱、老師、附件名稱或關鍵字，我才能縮小查詢範圍。"
        intent_context = json.dumps(
            {
                "mode": intent.mode,
                "month": intent.month,
                "terms": intent.terms,
                "attachment_names": intent.attachment_names,
                "broad": intent.broad,
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
                usage_key=artist.page_id,
            )
        except course_qa.CourseQAError as exc:
            logger.warning("課程助教未能回答：%s", exc)
            return exc.user_message
        except Exception:
            logger.exception("課程助教回答階段發生未預期錯誤")
            return "⚠️ 課程助教這次未能完成回答，請稍後重試。"
        self._memory_by_user[str(user.id)] = {
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

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """課程助教頻道的自然語言入口；每位藝人使用自己的私人 Thread。"""
        if message.author.bot or not self._is_ai_context(message.channel):
            return
        try:
            question = message.content.strip()
            if self.bot.user is not None:
                question = re.sub(
                    rf"<@!?{self.bot.user.id}>\s*", "", question
                ).strip()
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
                # 先移除公開問題，避免建立私人 Thread 或 AI 回答期間曝光內容。
                try:
                    await message.delete()
                except (discord.Forbidden, discord.HTTPException):
                    logger.warning("課程助教無法刪除公開問題，請補上管理訊息權限")

                thread = await self._get_or_create_thread(message)
                if thread is None:
                    try:
                        await message.author.send(
                            "❌ 無法建立私人課程助教對話，請確認 Bot 有「建立私人 Thread」與「管理訊息」權限。"
                        )
                    except (discord.Forbidden, discord.HTTPException):
                        logger.exception("課程助教建立私人對話失敗，且無法私訊使用者")
                    return

            if thread.id not in self._catalog_sent_threads:
                self._catalog_sent_threads.add(thread.id)
                catalog = await self._attachment_catalog_for_user(message.author)
                if catalog:
                    await thread.send(catalog)

            waiting = await thread.send("⏳ 請稍等一下喔，我看看……")
            answer = await self._answer_for_user(message.author, question)
            chunks = _split_discord_message(answer)
            artist = await repositories.find_artist_by_discord_id(message.author.id)
            view = AIAnswerFeedbackView(
                artist.name if artist else str(message.author),
                message.author.id,
                waiting.jump_url,
            )
            await waiting.edit(content=chunks[0], view=view)
            for chunk in chunks[1:]:
                await thread.send(chunk)
        except Exception:
            logger.exception("課程助教訊息處理失敗")
            try:
                await message.channel.send("⚠️ 課程助教處理失敗，請稍後再試；Bot 仍在運作。")
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("課程助教錯誤通知也發送失敗")

async def setup(bot: commands.Bot):
    await bot.add_cog(CourseQACog(bot))
