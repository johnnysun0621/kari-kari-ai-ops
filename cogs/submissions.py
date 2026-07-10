"""P0-3 作業提交：/提交任務（檔案自動歸檔 Drive；連結／文字走表單）。"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from cogs import channel_guard, ui_text
from services.permissions import is_admin
from services import audit, drive_api, drive_archive, repositories
from services.notion_api import NotionAPIError

logger = logging.getLogger(__name__)

LINK_PREFIXES = ("http://", "https://")
FREE_DISCORD_UPLOAD_MB = 10
MAX_UPLOAD_MB = FREE_DISCORD_UPLOAD_MB  # 標準流程假設藝人使用 Discord 免費方案
FILE_UPLOAD_WAIT_MINUTES = 180
REVIEW_RESULTS = ("通過", "需修改", "退回")


@dataclass
class PendingFileSubmission:
    task: repositories.TaskItem
    artist: repositories.Artist
    channel_id: int | None
    created_at: datetime
    expires_at: datetime

    def is_expired(self, now: datetime) -> bool:
        return now > self.expires_at


def submission_type_for_input(link_url: str, content: str) -> str | None:
    """依實際填寫內容決定 Submissions.提交類型；兩者皆空回 None（呼叫端擋下）。"""
    if link_url and content:
        return "混合"
    if link_url:
        return "Drive 連結"
    if content:
        return "文字"
    return None


def is_valid_link(link_url: str) -> bool:
    return link_url.startswith(LINK_PREFIXES) and " " not in link_url


def direct_upload_size_error(size_bytes: int) -> str | None:
    """回傳檔案大小錯誤訊息；None 表示可走 Discord 直接上傳。

    對外 SOP 以免費 Discord 帳號為準：10MB 以上改走公司 Drive / 雲端連結。
    Nitro 可上傳更大檔，但不作為標準營運流程。
    """
    if size_bytes <= MAX_UPLOAD_MB * 1024 * 1024:
        return None
    return (
        f"❌ 檔案超過 {MAX_UPLOAD_MB}MB。標準流程假設藝人使用 Discord 免費方案，"
        "10MB 以上請先上傳到公司指定 Google Drive / 自己的雲端，"
        "再用 `/提交任務` 貼 Drive 連結。"
    )


def file_upload_wait_label(minutes: int = FILE_UPLOAD_WAIT_MINUTES) -> str:
    if minutes % 60 == 0:
        return f"{minutes // 60} 小時"
    return f"{minutes} 分鐘"


def review_task_status_for_result(result: str) -> str | None:
    if result == "通過":
        return "已完成"
    if result in ("需修改", "退回"):
        return "需修改"
    return None


def review_dm_text(
    submission: repositories.SubmissionItem,
    *,
    task_name: str,
    result: str,
    feedback: str = "",
    reviewer: str = "",
) -> str:
    header = {
        "通過": "✅ 你的提交已通過",
        "需修改": "🛠 你的提交需要修改",
        "退回": "↩️ 你的提交已退回",
    }.get(result, f"審核結果：{result}")
    lines = [f"{header}：**{task_name or submission.name}**"]
    if reviewer:
        lines.append(f"審核者：{reviewer}")
    if feedback:
        lines.append(f"回饋：{feedback}")
    if result in ("需修改", "退回"):
        lines.append("請依回饋修改後，再到 `/提交任務` 重新提交。")
    return "\n".join(lines)


SUBMIT_METHOD_LABELS = {
    "檔案": "📎 交檔案",
    "Drive 連結": "🔗 貼連結",
    "文字": "📝 填文字",
    "混合": "📎🔗 檔案或連結",
}


def submit_method_label(method: str | None) -> str:
    return SUBMIT_METHOD_LABELS.get(method or "", "🔗📝 連結或文字")


def submission_method_conflict(task: repositories.TaskItem, has_attachment: bool) -> str | None:
    """依 Notion assign 的提交方式強制引導；回傳擋下訊息，None＝可以進行。

    檔案任務沒帶附件時，會改走同頻道等待附件流程，不再要求重打指令。
    """
    method = task.submit_method
    if has_attachment:
        if method in ("Drive 連結", "文字"):
            return (
                f"❌ 「{task.name}」在 Notion 指定的提交方式是**{method}**，不收檔案上傳。\n"
                "請重新打 `/提交任務`（不帶檔案），"
                + ("貼上你的雲端連結。" if method == "Drive 連結" else "直接填文字內容。")
            )
        return None
    return None


def modal_field_spec(method: str | None) -> tuple[bool, bool, bool, bool]:
    """依 assign 決定表單欄位：(顯示連結欄, 連結必填, 顯示文字欄, 文字必填)。"""
    if method == "Drive 連結":
        return True, True, True, False   # 連結必填，文字當補充說明
    if method == "文字":
        return False, False, True, True  # 只收文字
    return True, False, True, False      # 混合／未指定：至少填一項（on_submit 驗證）


def submit_hint_for_task(task: repositories.TaskItem) -> str:
    """Modal 開頭給藝人的提示：依任務的提交方式引導。"""
    method = task.submit_method
    if method == "檔案":
        return (
            f"此任務要求交檔案：選定後請直接在同一個頻道上傳附件。"
            f"標準直接上傳上限為 {MAX_UPLOAD_MB}MB；大檔請貼 Drive 連結。"
        )
    if method == "Drive 連結":
        return "請貼上作品的雲端連結（記得開啟共用權限）。"
    if method == "文字":
        return "請直接填寫文字內容。"
    return "可貼雲端連結、填文字內容，或兩者都填。"


class SubmissionModal(discord.ui.Modal):
    """提交表單：連結＋文字皆選填，但至少要填一項。"""

    def __init__(self, task: repositories.TaskItem):
        super().__init__(title=f"提交｜{task.name}"[:45])
        self.task = task
        show_link, link_required, show_content, content_required = modal_field_spec(task.submit_method)
        self.link_input: discord.ui.TextInput | None = None
        self.content_input: discord.ui.TextInput | None = None
        if show_link:
            self.link_input = discord.ui.TextInput(
                label="作品連結" + ("（必填）" if link_required else "（選填）"),
                placeholder="https://...",
                required=link_required,
                max_length=500,
            )
            self.add_item(self.link_input)
        if show_content:
            self.content_input = discord.ui.TextInput(
                label="文字內容／說明" + ("（必填）" if content_required else "（選填）"),
                style=discord.TextStyle.paragraph,
                placeholder=submit_hint_for_task(task)[:100],
                required=content_required,
                max_length=1500,
            )
            self.add_item(self.content_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        link_url = (self.link_input.value or "").strip() if self.link_input else ""
        content = (self.content_input.value or "").strip() if self.content_input else ""

        submit_type = submission_type_for_input(link_url, content)
        if submit_type is None:
            await interaction.followup.send(
                "❌ 連結和文字內容至少要填一項，請再按一次「提交任務」。", ephemeral=True
            )
            return
        if link_url and not is_valid_link(link_url):
            await interaction.followup.send(
                "❌ 連結格式看起來不對（要以 http:// 或 https:// 開頭），請確認後重新提交。",
                ephemeral=True,
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
            # 先取消舊版最新標記，再建立新提交（順序反了會把新的一起取消）
            replaced = await repositories.mark_previous_submissions_not_latest(self.task.page_id)
            submission_page = await repositories.create_submission(
                self.task, artist,
                submit_type=submit_type, link_url=link_url, content=content,
            )
            latest_url = link_url or submission_page.get("url", "")
            await repositories.set_task_under_review(self.task.page_id, latest_url)
        except Exception:
            logger.exception("建立提交失敗")
            await interaction.followup.send("❌ 寫入 Notion 失敗，請稍後再試或聯絡管理者。", ephemeral=True)
            await audit.log_event(
                event_name=f"提交任務失敗｜{artist.name}｜{self.task.name}",
                action_type="錯誤", operator=artist.name,
                discord_user_id=str(interaction.user.id),
                target_db="Submissions & File Index", result="失敗",
                error_message="create_submission exception（詳見 bot log）",
            )
            return

        await audit.log_event(
            event_name=f"提交任務｜{artist.name}｜{self.task.name}",
            action_type="提交", operator=artist.name,
            discord_user_id=str(interaction.user.id),
            target_db="Submissions & File Index",
            target_url=submission_page.get("url", ""),
            after=f"{submit_type}｜任務轉待審核" + (f"｜取代 {replaced} 筆舊版" if replaced else ""),
        )

        # 通知控方（營運內部）。通知失敗只記 log，不影響藝人的提交確認
        internal = interaction.client.get_channel(config.ADMIN_NOTIFY_CHANNEL_ID)
        link_part = f"\n連結：{link_url}" if link_url else ""
        await channel_guard.try_send(
            internal,
            content=(
                f"📥 **新提交待審核**\n"
                f"任務：{self.task.name}｜{artist.name}｜{submit_type}"
                f"{link_part}\nNotion：{submission_page.get('url', '')}"
            ),
        )

        resubmit_note = f"\n（已取代先前 {replaced} 筆提交，這筆為最新版）" if replaced else ""
        await interaction.followup.send(
            f"✅ 已提交：**{self.task.name}**（{submit_type}）\n"
            f"任務狀態已轉「待審核」，審核結果會再通知你。{resubmit_note}",
            ephemeral=True,
        )


async def _archive_file_submission(
    *,
    client: discord.Client,
    task: repositories.TaskItem,
    artist: repositories.Artist,
    attachment: discord.Attachment,
    discord_user_id: int,
) -> tuple[bool, str]:
    """檔案提交核心：下載附件 → 歸檔 Drive → 寫 Notion → 通知控方。"""
    size_error = direct_upload_size_error(attachment.size)
    if size_error:
        return False, size_error
    now = datetime.now(config.get_tenant().tz)
    target = drive_archive.resolve_archive_target(task, artist.drive_folder_url, now)
    if target is None:
        return False, (
            "❌ 你的藝人專區資料夾還沒設定（Notion Artists 的「Google Drive 資料夾」），\n"
            "請聯絡營運；或改用 `/提交任務` 貼雲端連結。"
        )
    base_folder_id, path_segments = target

    try:
        content = await attachment.read()
        uploaded = await drive_api.upload_submission_file(
            base_folder_id=base_folder_id,
            path_segments=path_segments,
            filename=attachment.filename,
            content=content,
            mime_type=attachment.content_type or "",
        )
    except Exception:
        logger.exception("Drive 上傳失敗")
        return False, (
            "❌ 檔案上傳 Google Drive 失敗（未寫入任何紀錄）。\n"
            "請稍後再試，或改用 `/提交任務` 貼雲端連結。"
        )

    drive_url = uploaded.get("webViewLink", "")
    file_size_mb = round(attachment.size / 1048576, 2)
    try:
        replaced = await repositories.mark_previous_submissions_not_latest(task.page_id)
        submission_page = await repositories.create_submission(
            task, artist,
            submit_type="檔案",
            link_url=drive_url,
            drive_file_id=uploaded.get("id", ""),
            original_filename=attachment.filename,
            file_size_mb=file_size_mb,
        )
        await repositories.set_task_under_review(task.page_id, drive_url)
    except Exception:
        logger.exception("檔案提交寫入 Notion 失敗")
        return False, f"⚠️ 檔案已上傳到 Drive（{drive_url}），但 Notion 紀錄寫入失敗，請聯絡營運補登。"

    folder_text = "／".join(path_segments) if path_segments else "任務指定資料夾"
    await audit.log_event(
        event_name=f"提交任務（檔案）｜{artist.name}｜{task.name}",
        action_type="上傳", operator=artist.name,
        discord_user_id=str(discord_user_id),
        target_db="Submissions & File Index",
        target_url=submission_page.get("url", ""),
        after=f"{attachment.filename}｜{file_size_mb}MB｜{folder_text}"
              + (f"｜取代 {replaced} 筆舊版" if replaced else ""),
    )
    internal = client.get_channel(config.ADMIN_NOTIFY_CHANNEL_ID)
    await channel_guard.try_send(
        internal,
        content=(
            f"📥 **新提交待審核（檔案）**\n"
            f"任務：{task.name}｜{artist.name}｜{attachment.filename}（{file_size_mb}MB）\n"
            f"Drive：{drive_url}\nNotion：{submission_page.get('url', '')}"
        ),
    )
    resubmit_note = f"\n（已取代先前 {replaced} 筆提交，這筆為最新版）" if replaced else ""
    return True, (
        f"✅ 檔案已提交並歸檔：**{task.name}**\n"
        f"📁 {artist.name}專區／{folder_text}／{attachment.filename}\n"
        f"任務狀態已轉「待審核」，審核結果會再通知你。{resubmit_note}"
    )


async def _handle_file_submission(
    interaction: discord.Interaction,
    task: repositories.TaskItem,
    attachment: discord.Attachment,
):
    """Slash 附件 fallback：保留舊入口，但與同頻道附件共用核心提交流程。"""
    await interaction.response.defer(ephemeral=True, thinking=True)
    artist = await repositories.find_artist_by_discord_id(interaction.user.id)
    if artist is None:
        await interaction.followup.send(
            "❌ 找不到你的藝人資料，請聯絡管理者確認 Notion 上的 Discord User ID 設定。",
            ephemeral=True,
        )
        return
    ok, message = await _archive_file_submission(
        client=interaction.client,
        task=task,
        artist=artist,
        attachment=attachment,
        discord_user_id=interaction.user.id,
    )
    await interaction.followup.send(message, ephemeral=True)


class SubmitTaskSelect(discord.ui.Select):
    def __init__(
        self,
        cog: "SubmissionsCog",
        tasks: list[repositories.TaskItem],
        now: datetime,
        attachment: discord.Attachment | None = None,
    ):
        self.cog = cog
        self.task_by_id = {t.page_id: t for t in tasks}
        self.attachment = attachment
        options = [
            discord.SelectOption(
                label=t.name[:100],
                value=t.page_id,
                description=(
                    f"{submit_method_label(t.submit_method)}｜{t.task_type or '任務'}｜{ui_text.deadline_text(t, now)}"
                )[:100],
            )
            for t in tasks[:25]
        ]
        super().__init__(placeholder="選擇要提交的任務…", options=options)

    async def callback(self, interaction: discord.Interaction):
        task = self.task_by_id[self.values[0]]
        conflict = submission_method_conflict(task, self.attachment is not None)
        if conflict:
            await interaction.response.send_message(conflict, ephemeral=True)
            return
        if self.attachment is not None:
            await _handle_file_submission(interaction, task, self.attachment)
            return
        if task.submit_method == "檔案":
            await self.cog.start_file_upload_wait(interaction, task)
            return
        if task.submit_method == "混合":
            await interaction.response.send_message(
                f"「{task.name}」可交檔案或貼連結／文字，請選擇提交方式：",
                view=MixedSubmissionChoiceView(self.cog, task),
                ephemeral=True,
            )
            return
        # 連結/文字：Modal 必須是元件互動的第一個回應
        await interaction.response.send_modal(SubmissionModal(task))


class SubmitTaskView(discord.ui.View):
    def __init__(
        self,
        cog: "SubmissionsCog",
        tasks: list[repositories.TaskItem],
        now: datetime,
        attachment: discord.Attachment | None = None,
    ):
        super().__init__(timeout=600)
        self.add_item(SubmitTaskSelect(cog, tasks, now, attachment))


class MixedSubmissionChoiceView(discord.ui.View):
    def __init__(self, cog: "SubmissionsCog", task: repositories.TaskItem):
        super().__init__(timeout=300)
        self.cog = cog
        self.task = task

    @discord.ui.button(label="上傳檔案", style=discord.ButtonStyle.primary)
    async def upload_file(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.start_file_upload_wait(interaction, self.task)

    @discord.ui.button(label="貼連結／文字", style=discord.ButtonStyle.secondary)
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SubmissionModal(self.task))


class ReviewSubmissionSelect(discord.ui.Select):
    def __init__(self, cog: "SubmissionsCog", submissions: list[repositories.SubmissionItem]):
        self.cog = cog
        self.submission_by_id = {submission.page_id: submission for submission in submissions}
        options = [
            discord.SelectOption(
                label=submission.name[:100],
                value=submission.page_id,
                description=(f"{submission.submit_type or '提交'}｜{submission.status or '待審核'}")[:100],
            )
            for submission in submissions[:25]
        ]
        super().__init__(placeholder="選擇要審核的提交…", options=options)

    async def callback(self, interaction: discord.Interaction):
        submission = self.submission_by_id[self.values[0]]
        await interaction.response.send_message(
            f"審核：**{submission.name}**\n請選擇結果，下一步可填回饋給藝人。",
            view=ReviewActionView(self.cog, submission, interaction.user.id),
            ephemeral=True,
        )


class ReviewSubmissionView(discord.ui.View):
    def __init__(self, cog: "SubmissionsCog", submissions: list[repositories.SubmissionItem]):
        super().__init__(timeout=600)
        self.add_item(ReviewSubmissionSelect(cog, submissions))


class ReviewActionView(discord.ui.View):
    def __init__(self, cog: "SubmissionsCog", submission: repositories.SubmissionItem, reviewer_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.submission = submission
        self.reviewer_id = reviewer_id

    async def _ensure_reviewer(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.reviewer_id:
            return True
        await interaction.response.send_message("❌ 這個審核按鈕只給原本操作的人使用。", ephemeral=True)
        return False

    @discord.ui.button(label="通過", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._ensure_reviewer(interaction):
            await interaction.response.send_modal(SubmissionReviewModal(self.cog, self.submission, "通過"))

    @discord.ui.button(label="需修改", style=discord.ButtonStyle.primary)
    async def needs_fix(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._ensure_reviewer(interaction):
            await interaction.response.send_modal(SubmissionReviewModal(self.cog, self.submission, "需修改"))

    @discord.ui.button(label="退回", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._ensure_reviewer(interaction):
            await interaction.response.send_modal(SubmissionReviewModal(self.cog, self.submission, "退回"))


class SubmissionReviewModal(discord.ui.Modal):
    def __init__(self, cog: "SubmissionsCog", submission: repositories.SubmissionItem, result: str):
        super().__init__(title=f"審核｜{result}"[:45])
        self.cog = cog
        self.submission = submission
        self.result = result
        self.feedback_input = discord.ui.TextInput(
            label="給藝人的回饋（選填）",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1500,
            placeholder="例如：這次節奏很穩；或請重修副歌音準。",
        )
        self.add_item(self.feedback_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.complete_discord_review(
            interaction,
            self.submission,
            self.result,
            (self.feedback_input.value or "").strip(),
        )


class SubmissionsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.pending_file_submissions: dict[int, PendingFileSubmission] = {}
        self._review_notified: set[str] = set()
        self.review_poll_loop.start()

    def cog_unload(self):
        self.review_poll_loop.cancel()

    async def start_file_upload_wait(
        self,
        interaction: discord.Interaction,
        task: repositories.TaskItem,
    ) -> None:
        """選到檔案任務後，在目前頻道等待附件，避免要求藝人跳出或重打指令。"""
        artist = await repositories.find_artist_by_discord_id(interaction.user.id)
        if artist is None:
            await interaction.response.send_message(
                "❌ 找不到你的藝人資料，請聯絡管理者確認 Notion 上的 Discord User ID 設定。",
                ephemeral=True,
            )
            return
        now = datetime.now(config.get_tenant().tz)
        expires_at = now + timedelta(minutes=FILE_UPLOAD_WAIT_MINUTES)
        self.pending_file_submissions[interaction.user.id] = PendingFileSubmission(
            task=task,
            artist=artist,
            channel_id=interaction.channel_id,
            created_at=now,
            expires_at=expires_at,
        )
        await interaction.response.send_message(
            f"📎 請直接在這個頻道上傳 **{task.name}** 的檔案。\n"
            f"檔案請在 **{MAX_UPLOAD_MB}MB 以下**；超過 {MAX_UPLOAD_MB}MB 請先上傳到 Drive，再用 `/提交任務` 貼連結。\n"
            f"我會在這個頻道等待你的下一個附件，入口 **{file_upload_wait_label()}**後失效。",
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        pending = self.pending_file_submissions.get(message.author.id)
        if pending is None:
            return
        if pending.channel_id is not None and message.channel.id != pending.channel_id:
            return
        now = datetime.now(config.get_tenant().tz)
        if pending.is_expired(now):
            self.pending_file_submissions.pop(message.author.id, None)
            await message.channel.send("❌ 這次檔案上傳入口已過期，請重新執行 `/提交任務`。")
            return
        if not message.attachments:
            await message.channel.send(
                "我目前沒有讀到附件。請直接上傳一個檔案附件；如果你剛剛已經上傳圖片但我仍讀不到，"
                "代表 Discord Bot 的 Message Content Intent 尚未開啟，請通知控方處理。"
            )
            return
        if len(message.attachments) > 1:
            await message.channel.send("一次請提交一個檔案；多個檔案請先壓成一個 zip，或改用 Drive 資料夾連結。")
            return
        attachment = message.attachments[0]
        size_error = direct_upload_size_error(attachment.size)
        if size_error:
            await message.channel.send(size_error)
            return
        if not drive_api.is_configured():
            await message.channel.send("❌ Drive 歸檔尚未設定，請先改用 Drive 連結提交。")
            return
        self.pending_file_submissions.pop(message.author.id, None)
        await message.channel.send("收到檔案，正在歸檔到 Google Drive 並寫入 Notion...")
        ok, result_message = await _archive_file_submission(
            client=self.bot,
            task=pending.task,
            artist=pending.artist,
            attachment=attachment,
            discord_user_id=message.author.id,
        )
        await message.channel.send(result_message)

    async def _notify_review_result(
        self,
        submission: repositories.SubmissionItem,
        *,
        result: str,
        feedback: str = "",
        reviewer: str = "",
    ) -> int:
        artist_map = await repositories.get_artist_map()
        sent = 0
        task_name = submission.name
        for artist_page_id in submission.artist_page_ids:
            artist = artist_map.get(artist_page_id)
            if not artist or not artist.discord_user_id:
                continue
            try:
                user = await self.bot.fetch_user(int(artist.discord_user_id))
                await user.send(
                    review_dm_text(
                        submission,
                        task_name=task_name,
                        result=result,
                        feedback=feedback,
                        reviewer=reviewer,
                    )
                )
                sent += 1
            except Exception:
                logger.exception("審核結果 DM 失敗（submission=%s, artist=%s）", submission.page_id, artist.name)
        return sent

    async def _update_review_task_status(self, submission: repositories.SubmissionItem, result: str) -> None:
        task_status = review_task_status_for_result(result)
        if not task_status:
            return
        for task_page_id in submission.task_page_ids:
            await repositories.update_task_status(task_page_id, task_status)

    async def complete_discord_review(
        self,
        interaction: discord.Interaction,
        submission: repositories.SubmissionItem,
        result: str,
        feedback: str,
    ) -> None:
        now = datetime.now(config.get_tenant().tz)
        reviewer = f"{interaction.user.display_name}｜{interaction.user.id}"
        try:
            await repositories.update_submission_review(
                submission.page_id,
                status=result,
                feedback=feedback,
                reviewer=reviewer,
                source="Discord",
                notified_at=now,
            )
            await self._update_review_task_status(submission, result)
        except NotionAPIError:
            logger.exception("審核寫入 Notion 失敗")
            await interaction.followup.send(
                "❌ 審核寫入 Notion 失敗。請確認 Submissions 已新增："
                "`審核回饋`、`審核者`、`審核來源`、`審核通知時間`。",
                ephemeral=True,
            )
            return
        sent = await self._notify_review_result(
            submission,
            result=result,
            feedback=feedback,
            reviewer=reviewer,
        )
        self._review_notified.add(submission.page_id)
        await audit.log_event(
            event_name=f"審核提交｜{submission.name}",
            action_type="審核", operator=interaction.user.display_name,
            discord_user_id=str(interaction.user.id),
            target_db="Submissions & File Index", target_url=submission.url,
            after=f"{result}｜通知 {sent} 位藝人",
        )
        await interaction.followup.send(
            f"✅ 已審核：**{submission.name}** → {result}\n"
            f"已通知藝人：{sent} 位",
            ephemeral=True,
        )

    @tasks.loop(minutes=1)
    async def review_poll_loop(self):
        try:
            submissions = await repositories.get_pending_review_notifications()
        except Exception:
            logger.exception("讀取待通知審核結果失敗")
            return
        for submission in submissions:
            if submission.page_id in self._review_notified:
                continue
            if submission.status not in REVIEW_RESULTS:
                continue
            feedback = submission.review_feedback
            reviewer = submission.reviewer or "老師／控方"
            try:
                await self._update_review_task_status(submission, submission.status)
                sent = await self._notify_review_result(
                    submission,
                    result=submission.status,
                    feedback=feedback,
                    reviewer=reviewer,
                )
                self._review_notified.add(submission.page_id)
                try:
                    await repositories.mark_submission_review_notified(
                        submission.page_id,
                        source=submission.review_source or "Notion",
                        notified_at=datetime.now(config.get_tenant().tz),
                    )
                except NotionAPIError:
                    logger.exception("審核通知時間回寫失敗，請確認 Submissions 審核欄位已建立")
                await audit.log_event(
                    event_name=f"審核結果通知｜{submission.name}",
                    action_type="通知", operator="System", source="Notion",
                    target_db="Submissions & File Index", target_url=submission.url,
                    after=f"{submission.status}｜通知 {sent} 位藝人",
                )
            except Exception:
                logger.exception("審核結果通知失敗（submission=%s）", submission.page_id)

    @review_poll_loop.before_loop
    async def before_review_poll(self):
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="提交任務",
        description="依 Notion 指定方式提交作業／作品，任務會轉待審核",
    )
    @app_commands.describe(檔案=f"Notion 指定交檔案時使用；超過 {MAX_UPLOAD_MB}MB 請改貼雲端連結")
    async def submit_task(
        self,
        interaction: discord.Interaction,
        檔案: discord.Attachment | None = None,
    ):
        if not await channel_guard.ensure_task_channel(interaction):
            return
        if 檔案 is not None:
            size_error = direct_upload_size_error(檔案.size)
            if size_error:
                await interaction.response.send_message(
                    size_error,
                    ephemeral=True,
                )
                return
            if not drive_api.is_configured():
                await interaction.response.send_message(
                    "❌ Drive 歸檔尚未設定（見 DRIVE_OAUTH_SETUP.md），請先貼雲端連結提交。",
                    ephemeral=True,
                )
                return
        await interaction.response.defer(ephemeral=True, thinking=True)
        artist = await repositories.find_artist_by_discord_id(interaction.user.id)
        if artist is None:
            await interaction.followup.send(
                "❌ 找不到你的藝人資料，請聯絡管理者確認 Notion 上的 Discord User ID 設定。",
                ephemeral=True,
            )
            return
        try:
            tasks = await repositories.get_discord_tasks_for_artist(artist.page_id)
        except Exception:
            logger.exception("讀取可提交任務失敗")
            await interaction.followup.send("❌ 讀取任務失敗，請稍後再試。", ephemeral=True)
            return
        submittable = [t for t in tasks if repositories.is_task_submittable(t)]
        if not submittable:
            await interaction.followup.send(
                "目前沒有開放提交的任務。\n"
                "（任務需在 Notion 勾「是否開放提交」且尚未進入審核／結案狀態）",
                ephemeral=True,
            )
            return
        now = datetime.now(config.get_tenant().tz)
        submittable.sort(key=lambda t: (t.end is None, t.end or now))
        if 檔案 is not None:
            hint = f"已附檔案 **{檔案.filename}**——選擇任務後會自動歸檔到你的專區。"
        else:
            hint = (
                "下方會顯示每個任務在 Notion 指定的提交方式。"
                "選擇後 Bot 會自動開啟對應表單；若任務要求交檔案，請直接在同一個頻道上傳附件。"
            )
        await interaction.followup.send(
            f"📤 {artist.name} 可提交的任務（{len(submittable)}）：\n{hint}",
            view=SubmitTaskView(self, submittable, now, attachment=檔案),
            ephemeral=True,
        )

    @app_commands.command(name="審核提交", description="管理端：審核待審核提交並通知藝人")
    @app_commands.default_permissions(administrator=True)
    @is_admin()
    async def review_submission(self, interaction: discord.Interaction):
        if not await channel_guard.ensure_task_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            submissions = await repositories.get_reviewable_submissions()
        except Exception:
            logger.exception("讀取待審核提交失敗")
            await interaction.followup.send("❌ 讀取待審核提交失敗，請稍後再試。", ephemeral=True)
            return
        if not submissions:
            await interaction.followup.send("目前沒有待審核提交。", ephemeral=True)
            return
        await interaction.followup.send(
            f"📮 待審核提交（{len(submissions)} 筆，最多顯示 25 筆）：",
            view=ReviewSubmissionView(self, submissions),
            ephemeral=True,
        )

    async def cog_app_command_error(self, interaction: discord.Interaction, error):
        logger.exception("提交指令執行錯誤", exc_info=error)
        msg = "❌ 指令執行出錯，請稍後再試（詳見 bot log）。"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SubmissionsCog(bot))
