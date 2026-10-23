"""小律：藝人在 Discord 用自然語言查會議／課程紀錄（M6-06）與自己的任務（M6-03）。"""
import asyncio
import logging
import json
import re
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import commands

import config
from cogs import channel_guard, ui_text
from services import course_intent, course_qa, repositories, task_query
from services.notion_links import strip_notion_links

logger = logging.getLogger(__name__)
_THREAD_STATE_PATH = Path(".state/course_qa_threads.json")
_MEMORY_STATE_PATH = Path(".state/course_qa_memory.json")
_PANEL_STATE_PATH = Path(".state/xiaolu_panel.json")
START_BUTTON_ID = "xiaolu:start"
# 頻道置頂的入口說明。2026-10-18：藝人在頻道公開發問時，bot 刪除前那一兩秒（與通知預覽）
# 大家都看得到，所以入口改成按鈕，問題從頭到尾只出現在本人的私人討論串。
def _submit_channel() -> str:
    """交件的地方：有設定任務查詢入口就給可點的頻道連結。"""
    return f"<#{config.TASK_CHANNEL_ID}>" if config.TASK_CHANNEL_ID else "`任務查詢入口`"


def panel_text() -> str:
    return (
        "🤖 **小律｜查任務的地方**\n\n"
        "這裡可以問**你自己任務的細節**：\n"
        "・這週／下週有什麼任務、還有哪些沒交\n"
        "・某個任務要做什麼、要交什麼形式、什麼時候截止\n\n"
        "👉 按下方「💬 開始問小律」，會開一個**只有你看得到**的討論串，在裡面直接打字問就好。\n\n"
        "📤 **要交作業、按「收到任務」，不是在這裡**\n"
        f"請到 {_submit_channel()} 打 `/我的任務`。小律只能幫你查，不能幫你交。\n\n"
        "⚠️ 請不要直接在這個頻道打字，這裡大家都看得到。"
    )


def thread_welcome() -> str:
    return (
        "👋 這裡只有你看得到，直接打字問就好，例如：\n"
        "・「這週有什麼任務」「我還有哪些沒交」\n"
        "・「第 2 個的詳細內容」\n"
        "・「week4 聽力要做幾題」\n\n"
        f"📤 要交作業請到 {_submit_channel()} 打 `/我的任務`。"
    )
_DISCORD_MESSAGE_LIMIT = 1900
TASK_ONLY_HELP = (
    "🤖 **小律**\n\n"
    "我可以幫你查自己的任務，例如：\n"
    "・「這週有什麼任務」「下週要交什麼」「11 月有什麼行程」\n"
    "・「我還有哪些沒交」\n"
    "・「第 2 個的詳細內容」，或直接講任務名稱\n"
    "・「week4 聽力要做幾題」「要用什麼格式交」\n\n"
    "要交件或按「收到任務」，請用 `/我的任務`。"
)


def can_artist_use_ai(artist: repositories.Artist) -> bool:
    return artist.status not in repositories.INACTIVE_ARTIST_STATUSES


def _sanitize_memory(value: dict) -> dict:
    """只保留檢索狀態，不持久化使用者問題文字。"""
    allowed = {"mode", "month", "record_ids", "attachment_names", "task_ids"}
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


class AIAnswerFeedbackModal(discord.ui.Modal, title="回報小律的回答"):
    feedback = discord.ui.TextInput(
        label="問題說明",
        placeholder="例如：任務列錯、日期不對、漏了某筆任務，或內容不正確",
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
            logger.exception("小律問題回報寫入 Notion 失敗")
            await interaction.followup.send("❌ 回報寫入失敗，請稍後再試。", ephemeral=True)
            return
        await interaction.followup.send(
            "✅ 已寫入 Notion 布告欄的「🙋 使用回饋」，公司會在那裡追蹤。",
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


class StartXiaolu(discord.ui.View):
    """頻道置頂的入口按鈕；timeout=None ＋固定 custom_id，bot 重啟後舊訊息上的按鈕仍可按。"""

    def __init__(self, cog: "CourseQACog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="開始問小律", emoji="💬", style=discord.ButtonStyle.primary, custom_id=START_BUTTON_ID)
    async def start(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.open_private_thread(interaction)


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
        """啟動時預熱課程目錄；只讀 Notion，不呼叫 LLM。註冊入口按鈕並確保頻道有置頂說明。"""
        self._warm_task = asyncio.create_task(self._warm_course_cache())
        self.bot.add_view(StartXiaolu(self))
        self._panel_task = asyncio.create_task(self._ensure_panel())

    async def _ensure_panel(self) -> None:
        """頻道裡只放一則置頂入口；已存在就更新內容，被刪掉才重發。"""
        await self.bot.wait_until_ready()
        channel = self.bot.get_channel(config.AI_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            state = json.loads(_PANEL_STATE_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
            state = {}
        view = StartXiaolu(self)
        try:
            if state.get("channel_id") == channel.id and state.get("message_id"):
                try:
                    message = await channel.fetch_message(int(state["message_id"]))
                    if message.content != panel_text():
                        await message.edit(content=panel_text(), view=view)
                    if not message.pinned:
                        await message.pin(reason="小律入口")
                    return
                except discord.NotFound:
                    pass
            message = await channel.send(panel_text(), view=view)
            await message.pin(reason="小律入口")
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("小律入口置頂訊息建立失敗（需要傳送訊息與管理訊息權限）")
            return
        _PANEL_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PANEL_STATE_PATH.write_text(
            json.dumps({"channel_id": channel.id, "message_id": message.id}), encoding="utf-8",
        )

    async def open_private_thread(self, interaction: discord.Interaction) -> None:
        """按「開始問小律」：開（或帶回）本人的私人討論串。問題從頭到尾不經過公開頻道。"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        artist = await repositories.find_artist_by_discord_id(interaction.user.id)
        if artist is None:
            await interaction.followup.send("❌ 找不到你的藝人資料，請聯絡公司確認 Discord User ID。", ephemeral=True)
            return
        if not can_artist_use_ai(artist):
            await interaction.followup.send("❌ 目前的合約狀態無法使用小律，請聯絡公司。", ephemeral=True)
            return
        parent = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None
        if parent is None:
            await interaction.followup.send("❌ 請在小律頻道按這個按鈕。", ephemeral=True)
            return
        thread, created = await self._get_or_create_thread_for(interaction.user, parent)
        if thread is None:
            await interaction.followup.send(
                "❌ 無法建立私人討論串，請通知公司確認 Bot 有「建立私人討論串」權限。", ephemeral=True,
            )
            return
        if created:
            await thread.send(f"{interaction.user.mention} {thread_welcome()}")
        await interaction.followup.send(f"✅ 你的私人討論串在這裡：{thread.mention}", ephemeral=True)

    async def _warm_course_cache(self) -> None:
        await self.bot.wait_until_ready()
        if not config.get_tenant().meeting_qa_enabled:
            return
        try:
            for artist in await repositories.get_active_artists():
                records = await repositories.get_course_meetings_for_artist(artist.page_id)
                if artist.discord_user_id:
                    self._records_cache[int(artist.discord_user_id)] = records
            logger.info("小律目錄預熱完成：%d 位藝人", len(self._records_cache))
        except Exception:
            logger.exception("小律目錄預熱失敗，改用提問時讀取")

    def cog_unload(self):
        if self._warm_task:
            self._warm_task.cancel()
        if getattr(self, "_panel_task", None):
            self._panel_task.cancel()

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
            return None, None, "❌ 找不到你的藝人資料，請聯絡公司。"
        if not can_artist_use_ai(artist):
            return artist, None, "❌ 目前的合約狀態無法使用小律，請聯絡公司。"
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
            return artist, [], (
                "目前沒有可供你查詢的會議紀錄。請確認公司已勾選 Notion 的「藝人可見」。\n"
                "想查自己的任務可以直接問，例如「這週有什麼任務」「還有哪些沒交」。"
            )
        return artist, records, None

    async def _answer_for_user(self, user: discord.abc.User, question: str) -> str:
        """同一位使用者同時只跑一個回答，避免重複消耗 API 額度。"""
        lock = self._answer_locks.setdefault(user.id, asyncio.Lock())
        async with lock:
            return await self._answer_for_user_unlocked(user, question)

    async def _answer_for_user_unlocked(self, user: discord.abc.User, question: str) -> str:
        if not config.get_tenant().meeting_qa_enabled:
            # 會議紀錄查詢封存中：所有問題都走任務，對不到就說明可以怎麼問。
            return await self._answer_task_query(user, question) or TASK_ONLY_HELP
        if task_query.is_task_query(question) or (
            (task_query.wants_detail(question) or task_query.is_specific_question(question))
            and not task_query.is_meeting_question(question)
        ):
            answer = await self._answer_task_query(user, question)
            if answer is not None:
                return answer
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
            logger.exception("小律意圖判斷失敗，改用保守的本地檢索")
            intent = course_intent._fallback_intent(question, records)
        selected = course_intent.select_records(records, intent, memory)
        if not selected:
            return "請指定月份、會議主題、負責人、附件名稱或關鍵字，我才能縮小查詢範圍。"
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
        if intent.mode == "month_overview" and intent.month:
            answer = course_qa.build_month_overview_answer(selected, intent.month)
        else:
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
                logger.warning("小律未能回答：%s", exc)
                return exc.user_message
            except Exception:
                logger.exception("小律回答階段發生未預期錯誤")
                return "⚠️ 小律這次未能完成回答，請稍後重試。"
        self._memory_by_user[str(user.id)] = {
            "mode": intent.mode,
            "month": intent.month,
            "record_ids": [record.page_id for record in selected[:8]],
            "attachment_names": list(intent.attachment_names),
        }
        self._save_memory_state()
        return f"🤖 **小律**\n\n{answer}"

    async def _answer_task_query(self, user: discord.abc.User, question: str) -> str | None:
        """M6-03：用自然語言查自己的任務。只查詢、不經 LLM，範圍與 /我的任務 相同。

        問到某一筆（名稱或「第 N 個」）且想看內容時回任務詳細；
        只是想看內容、卻對不到任何任務時回 None，交回會議紀錄流程。
        """
        artist = await repositories.find_artist_by_discord_id(user.id)
        if artist is None:
            return "❌ 找不到你的藝人資料，請聯絡公司。"
        if not can_artist_use_ai(artist):
            return "❌ 目前的合約狀態無法使用小律，請聯絡公司。"
        try:
            tasks = await repositories.get_discord_tasks_for_artist(artist.page_id)
        except Exception:
            logger.exception("小律讀取任務失敗：%s", artist.name)
            return "❌ 讀取任務失敗，請稍後再試，或改用 `/我的任務`。"
        now = datetime.now(config.get_tenant().tz)
        detail_targets = self._detail_targets(user, question, tasks)
        specific = task_query.is_specific_question(question) and bool(config.get_tenant().llm_api_key)
        if detail_targets and specific:
            return await self._task_llm_answer(artist, question, detail_targets, now)
        if detail_targets:
            if len(detail_targets) == 1:
                return await self._task_detail_answer(detail_targets[0], now)
            names = "\n".join(f"・{strip_notion_links(task.name)}" for task in detail_targets[:10])
            return f"🤖 **小律**\n\n符合的任務有 {len(detail_targets)} 筆，你要看哪一筆？\n{names}"
        if not task_query.is_task_query(question):
            return None
        date_range = task_query.parse_range(question, now)
        pending_only = task_query.wants_pending_only(question)
        selected = task_query.select_tasks(tasks, date_range, pending_only=pending_only)
        scope = f"{date_range.label}" if date_range else "目前"
        kind = "還沒交的任務" if pending_only else "任務"
        footer = "想看某一筆的內容，可以問「第 2 個的詳細內容」或直接講任務名稱。要交件或按「收到任務」，請用 `/我的任務`。"
        if not selected:
            return f"🤖 **小律**\n\n{scope}沒有{kind}。\n{footer}"
        if specific and len(selected) <= task_query.MAX_CONTEXT_TASKS:
            return await self._task_llm_answer(artist, question, selected, now)
        lines = [
            f"・**{strip_notion_links(task.name)}**｜{task.task_type or '任務'}｜"
            f"{ui_text.deadline_text(task, now)}｜{task.status or '未填狀態'}"
            for task in selected[:task_query.MAX_LINES]
        ]
        self._memory_by_user[str(user.id)] = {
            **self._memory_by_user.get(str(user.id), {}),
            "task_ids": [task.page_id for task in selected[:task_query.MAX_LINES]],
        }
        self._save_memory_state()
        lines = [f"{index}. {line.removeprefix('・')}" for index, line in enumerate(lines, start=1)]
        more = len(selected) - len(lines)
        if more > 0:
            lines.append(f"（另有 {more} 筆，完整清單請用 `/我的任務`）")
        return (
            f"🤖 **小律**\n\n📑 **{artist.name}｜{scope}{'' if pending_only else '的'}{kind}**（{len(selected)} 筆）\n"
            + "\n".join(lines)
            + f"\n\n{footer}"
        )

    def _detail_targets(
        self, user: discord.abc.User, question: str, tasks: list[repositories.TaskItem],
    ) -> list[repositories.TaskItem]:
        """要看內容的那筆：先看「第 N 個」（上次列出的清單），再比對任務名稱。"""
        index = task_query.index_reference(question)
        if index is not None:
            remembered = self._memory_by_user.get(str(user.id), {}).get("task_ids") or []
            by_id = {task.page_id: task for task in tasks}
            if 1 <= index <= len(remembered) and remembered[index - 1] in by_id:
                return [by_id[remembered[index - 1]]]
        # 「這週有什麼任務」這種問法名稱不會中；問題裡點名某筆任務時就給那筆的詳細。
        return task_query.match_tasks_by_name(question, tasks)

    async def _task_body(self, task: repositories.TaskItem) -> str:
        """任務內頁整頁。查詢路徑大多已讀過（task.description），沒讀過的才補讀。"""
        if task.description:
            return task.description
        try:
            return await repositories.get_task_page_body(task.page_id)
        except Exception:
            logger.exception("小律讀取任務內頁失敗：%s", task.page_id)
            return ""

    async def _task_llm_answer(
        self,
        artist: repositories.Artist,
        question: str,
        tasks: list[repositories.TaskItem],
        now: datetime,
    ) -> str:
        """具體問題（幾題、什麼格式、要準備什麼）交給模型，只給這位藝人的這幾筆任務。"""
        tasks = tasks[:task_query.MAX_CONTEXT_TASKS]
        try:
            project_map = await repositories.get_project_map()
            person_ids = sorted({pid for task in tasks for pid in [*task.teacher_page_ids, *task.reviewer_page_ids]})
            people = {person.page_id: person.name for person in await repositories.get_people_items_by_ids(person_ids)}
        except Exception:
            logger.exception("小律讀取任務的專案／人員失敗，改用較少的欄位回答")
            project_map, people = {}, {}
        contexts = []
        for task in tasks:
            project = project_map.get(task.project_page_ids[0]) if task.project_page_ids else None
            contexts.append(task_query.build_task_context(
                task,
                now=now,
                deadline_text=ui_text.deadline_text(task, now),
                project_name=project.name if project else None,
                teacher_names=[people[pid] for pid in task.teacher_page_ids if pid in people],
                reviewer_names=[people[pid] for pid in task.reviewer_page_ids if pid in people],
                body=await self._task_body(task),
            ))
        try:
            answer = await course_qa.answer_task_question(
                artist.name, question, "\n\n---\n\n".join(contexts), usage_key=artist.page_id,
            )
        except course_qa.CourseQAError as exc:
            logger.warning("小律任務問答未能回答：%s", exc)
            if len(tasks) == 1:
                return await self._task_detail_answer(tasks[0], now)
            return exc.user_message
        names = "\n".join(f"・{strip_notion_links(task.name)}" for task in tasks)
        return (
            f"🤖 **小律**\n\n{answer}\n\n**參考任務**\n{names}\n\n"
            "要交件或按「收到任務」，請用 `/我的任務`。"
        )

    async def _task_detail_answer(self, task: repositories.TaskItem, now: datetime) -> str:
        """任務詳細：基本資訊＋任務內頁內容（有「任務說明」段落就用它，沒有就讀整頁）。"""
        body = await self._task_body(task)
        detail = task_query.task_detail_text(task, deadline_text=ui_text.deadline_text(task, now), body=body)
        return f"🤖 **小律**\n\n{detail}"

    async def _attachment_catalog_for_user(self, user: discord.abc.User) -> str | None:
        artist, records, error = await self._load_records_for_user(user)
        if error:
            return error
        pages = []
        for record in records or []:
            if not record.attachments:
                continue
            files = "、".join(attachment.name for attachment in record.attachments)
            # 這份索引是藝人開 Thread 時自動送出的第一則訊息，過去帶著 Notion 連結，
            # 等於不用發問就把入口送到手上。只留頁面名稱。
            pages.append(f"・{course_qa.source_label(record)}\n  檔案：{files}")
        if not pages:
            return None
        return (
            f"📚 **{artist.name} 的會議附件索引**\n\n"
            + "\n".join(pages)
            + "\n\n請直接說明你要查哪個會議記錄或檔案，再自由提問。"
        )

    async def _get_or_create_thread_for(
        self, user: discord.abc.User, parent: discord.TextChannel,
    ) -> tuple[discord.Thread | None, bool]:
        """回傳 (討論串, 是否新建)。舊的討論串被封存時重新打開，不另開一個。"""
        thread_id = self._thread_by_user.get(str(user.id))
        if thread_id:
            thread = self.bot.get_channel(thread_id)
            if thread is None:
                try:
                    thread = await self.bot.fetch_channel(thread_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    thread = None
            if isinstance(thread, discord.Thread):
                try:
                    if thread.archived:
                        await thread.edit(archived=False)
                    if thread.name.startswith("會議秘書小律"):  # 2026-10-18 改名前開的舊討論串
                        await thread.edit(name=thread.name.replace("會議秘書小律", "小律", 1))
                    await thread.add_user(user)
                    return thread, False
                except (discord.Forbidden, discord.HTTPException):
                    logger.warning("小律無法重新打開討論串 %s，改開新的", thread_id)
        try:
            thread = await parent.create_thread(
                name=f"小律｜{getattr(user, 'display_name', user.name)}"[:100],
                type=discord.ChannelType.private_thread,
                auto_archive_duration=1440,
                invitable=False,
                reason="小律私人對話（查任務）",
            )
            await thread.add_user(user)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("建立小律私人 Thread 失敗")
            return None, False
        self._thread_by_user[str(user.id)] = thread.id
        self._save_thread_state()
        return thread, True

    async def _get_or_create_thread(self, message: discord.Message) -> discord.Thread | None:
        if not isinstance(message.channel, discord.TextChannel):
            return None
        thread, _ = await self._get_or_create_thread_for(message.author, message.channel)
        return thread

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """小律頻道的自然語言入口；每位藝人使用自己的私人 Thread。"""
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
                    logger.warning("小律無法刪除公開問題，請補上管理訊息權限")

                thread = await self._get_or_create_thread(message)
                if thread is None:
                    try:
                        await message.author.send(
                            "❌ 無法建立私人的小律對話，請確認 Bot 有「建立私人 Thread」與「管理訊息」權限。"
                        )
                    except (discord.Forbidden, discord.HTTPException):
                        logger.exception("小律建立私人對話失敗，且無法私訊使用者")
                    return

            if not isinstance(message.channel, discord.Thread):
                await thread.send(
                    f"{message.author.mention} 你剛剛在公開頻道打的字已經收掉了。"
                    "下次請按頻道置頂的「💬 開始問小律」，或直接在這個討論串裡問，問題就不會被別人看到。"
                )
            if thread.id not in self._catalog_sent_threads and config.get_tenant().meeting_qa_enabled:
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
            logger.exception("小律訊息處理失敗")
            try:
                await message.channel.send("⚠️ 小律處理失敗，請稍後再試；Bot 仍在運作。")
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("小律錯誤通知也發送失敗")

async def setup(bot: commands.Bot):
    await bot.add_cog(CourseQACog(bot))
