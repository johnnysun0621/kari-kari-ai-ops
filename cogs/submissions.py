"""P0-3 作業提交：/提交任務（檔案自動歸檔 Drive；連結／文字走表單）。"""
import logging
import re
from dataclasses import dataclass, field
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
FILENAME_COMPONENT_LIMIT = 60
ORIGINAL_FILENAME_LIMIT = 120
_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>\|\r\n\t]+')
_WHITESPACE_CHARS = re.compile(r"\s+")


@dataclass
class PendingFileSubmission:
    task: repositories.TaskItem
    artist: repositories.Artist
    channel_id: int | None
    created_at: datetime
    expires_at: datetime
    folder_base_id: str = ""
    folder_path: list[str] = field(default_factory=list)
    folder_url: str = ""
    attachments: list[discord.Attachment] = field(default_factory=list)

    def is_expired(self, now: datetime) -> bool:
        return now > self.expires_at


@dataclass
class PendingReviewFeedbackFile:
    submission: repositories.SubmissionItem
    result: str
    feedback: str
    reviewer: str
    channel_id: int | None
    created_at: datetime
    expires_at: datetime
    targets: list["ReviewFeedbackDriveTarget"] = field(default_factory=list)
    attachments: list[discord.Attachment] = field(default_factory=list)

    def is_expired(self, now: datetime) -> bool:
        return now > self.expires_at


@dataclass(frozen=True)
class ReviewFeedbackDriveTarget:
    artist_page_id: str
    artist_name: str
    folder_url: str
    base_folder_id: str
    path_segments: list[str]


def expired_pending_keys(pending_by_user: dict, now: datetime) -> list:
    """回傳已過期的 pending 項目 key；純函式，供背景清理與測試共用。

    使用者若中途放棄上傳（從沒再傳訊息），pending 項目原本只在
    「使用者下次傳訊息」時才被動清掉，長期運行下會累積無主的 dead entry。
    """
    return [key for key, value in pending_by_user.items() if value.is_expired(now)]


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


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _safe_filename_part(value: str, *, fallback: str, limit: int = FILENAME_COMPONENT_LIMIT) -> str:
    text = _UNSAFE_FILENAME_CHARS.sub("_", value or "")
    text = _WHITESPACE_CHARS.sub(" ", text).strip(" ._")
    text = re.sub(r"_+", "_", text)
    text = text.replace("_.", ".")
    if not text:
        text = fallback
    return text[:limit].rstrip(" ._") or fallback


def _safe_original_filename(filename: str, *, limit: int = ORIGINAL_FILENAME_LIMIT) -> str:
    safe = _safe_filename_part(filename, fallback="file", limit=limit)
    if "." not in safe:
        return safe
    stem, ext = safe.rsplit(".", 1)
    if not stem or not ext:
        return safe[:limit].rstrip(" ._") or "file"
    suffix = f".{ext[:20]}"
    stem_limit = max(1, limit - len(suffix))
    return f"{stem[:stem_limit].rstrip(' ._') or 'file'}{suffix}"


def prefixed_submission_filename(
    *,
    artist_name: str,
    task_name: str,
    original_filename: str,
) -> str:
    """Drive 顯示檔名：藝人_任務_原檔名；Notion 另保留原始檔名。"""
    return "_".join(
        [
            _safe_filename_part(artist_name, fallback="artist"),
            _safe_filename_part(task_name, fallback="task"),
            _safe_original_filename(original_filename),
        ]
    )


def submission_folder_name(created_at: datetime) -> str:
    return f"提交_{created_at.strftime('%Y%m%d_%H%M')}"


def prefixed_review_feedback_filename(
    *,
    submission_name: str,
    original_filename: str,
) -> str:
    return "_".join(
        [
            "老師回饋",
            _safe_filename_part(submission_name, fallback="submission"),
            _safe_original_filename(original_filename),
        ]
    )


def large_file_drive_instruction(
    *,
    task_name: str,
    folder_url: str | None,
) -> str:
    lines = [
        f"大檔案請先上傳到 Google Drive，再用下方按鈕完成 **{task_name}** 的提交。",
    ]
    if folder_url:
        lines.append(f"指定上傳資料夾：{folder_url}")
        lines.append("上傳完成後，請按「我已上傳到 Drive」；若驗證失敗，也可按「貼 Drive 連結完成提交」。")
    else:
        lines.append("目前無法自動產生指定資料夾連結，請先把檔案上傳到你的雲端，再按「貼 Drive 連結完成提交」。")
    return "\n".join(lines)


def drive_folder_submission_summary(files: list[dict]) -> tuple[str, str, float]:
    """整理 Drive 驗證到的檔案資訊：(檔名摘要, File ID 摘要, 總 MB)。"""
    names = [str(item.get("name") or "未命名檔案") for item in files]
    ids = [str(item["id"]) for item in files if item.get("id")]
    total_bytes = sum(int(item.get("size") or 0) for item in files)
    return "、".join(names), "、".join(ids), round(total_bytes / 1048576, 2)


def review_feedback_drive_instruction(
    *,
    submission_name: str,
    targets: list[ReviewFeedbackDriveTarget],
    skipped_artists: list[str] | None = None,
) -> str:
    lines = [
        f"大批改檔請先上傳到 Google Drive，再用下方按鈕補到 **{submission_name}** 的審核回饋。",
    ]
    if targets:
        lines.append("指定老師回饋資料夾：")
        for target in targets[:5]:
            lines.append(f"- {target.artist_name}：{target.folder_url}")
        if len(targets) > 5:
            lines.append(f"- ...另 {len(targets) - 5} 個資料夾")
        lines.append("上傳完成後，請按「貼 Drive 連結補回饋」或直接上傳批改檔。")
    else:
        lines.append("目前無法自動產生老師回饋資料夾，請先把檔案上傳到可分享的雲端，再按「貼 Drive 連結補回饋」。")
    if skipped_artists:
        lines.append("未產生資料夾：" + "、".join(skipped_artists))
    return "\n".join(lines)


def notion_page_url(page_id: str) -> str:
    clean = (page_id or "").replace("-", "")
    return f"https://www.notion.so/{clean}" if clean else ""


def task_links_text(task_page_ids: list[str]) -> str:
    if not task_page_ids:
        return "無"
    links = []
    for idx, page_id in enumerate(task_page_ids[:5], start=1):
        links.append(f"[任務 {idx}]({notion_page_url(page_id)})")
    if len(task_page_ids) > 5:
        links.append(f"...另 {len(task_page_ids) - 5} 筆")
    return "、".join(links)


def review_submission_detail_text(submission: repositories.SubmissionItem) -> str:
    """審核前顯示提交內容摘要，讓老師不用先跳 Notion 才知道要審什麼。"""
    lines = [
        f"審核：**{submission.name}**",
        f"狀態：{submission.status or '待審核'}｜類型：{submission.submit_type or '提交'}",
    ]
    if submission.drive_url:
        lines.append(f"Drive：{submission.drive_url}")
    if submission.note:
        lines.append(f"文字摘要：{_clip(submission.note, 280)}")
    if submission.url:
        lines.append(f"Notion 提交頁：{submission.url}")
    lines.append(f"關聯任務：{task_links_text(submission.task_page_ids)}")
    lines.append("請選擇結果；送出審核後可再附批改檔給藝人。")
    return "\n".join(lines)


def feedback_with_attachment_links(feedback: str, file_links: list[str]) -> str:
    lines = [feedback.strip()] if feedback.strip() else []
    if file_links:
        lines.append("批改檔：" + "、".join(file_links))
    return "\n".join(lines)


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


def review_feedback_file_dm_text(
    submission: repositories.SubmissionItem,
    *,
    result: str,
    feedback: str,
    reviewer: str,
    file_links: list[str],
) -> str:
    file_text = "、".join(file_links) if file_links else "批改檔已補充"
    lines = [
        f"📎 老師補充了批改檔：**{submission.name}**",
        f"審核結果：{result}",
    ]
    if reviewer:
        lines.append(f"審核者：{reviewer}")
    if feedback:
        lines.append(f"回饋：{feedback}")
    lines.append(f"批改檔：{file_text}")
    return "\n".join(lines)


SUBMIT_METHOD_LABELS = {
    "檔案": "📎 交檔案",
    "Drive 連結": "🔗 貼連結",
    "文字": "📝 填文字",
    "混合": "📎🔗 檔案或連結",
}


def submit_method_label(method: str | None) -> str:
    return SUBMIT_METHOD_LABELS.get(method or "", "🔗📝 連結或文字")


def submit_task_option_description(task: repositories.TaskItem, now: datetime) -> str:
    """Select option 描述：第一眼就知道這筆任務要交什麼。"""
    return (
        f"{submit_method_label(task.submit_method)}｜"
        f"{task.task_type or '任務'}｜"
        f"{ui_text.deadline_text(task, now)}"
    )[:100]


def _teacher_requirement_text(task: repositories.TaskItem) -> str:
    description = _clip(task.description, 80)
    if description:
        return f"\n  老師要求：{description}"
    if task.submit_method == "檔案":
        return f"\n  老師要求：依 Notion 任務說明上傳指定檔案；超過 {MAX_UPLOAD_MB}MB 請改貼 Drive 連結。"
    return ""


def submit_task_intro_text(
    artist_name: str,
    tasks: list[repositories.TaskItem],
    now: datetime,
) -> str:
    """提交第一關文案：先讓藝人知道有哪些任務、各自要交什麼。"""
    lines = [
        f"📤 {artist_name} 可提交的任務（{len(tasks)}）：",
        "先選任務；選定後我才會依 Notion 指定方式要求你上傳檔案、貼連結或填文字。",
        "",
    ]
    for task in tasks[:10]:
        lines.append(
            f"・{task.name}｜{submit_method_label(task.submit_method)}｜"
            f"{task.task_type or '任務'}｜{ui_text.deadline_text(task, now)}"
            f"{_teacher_requirement_text(task)}"
        )
    if len(tasks) > 10:
        lines.append(f"...另 {len(tasks) - 10} 筆請用下方選單查看")
    lines.append("")
    lines.append("檔案任務：選任務後直接在這個頻道上傳附件；10MB 以上請用 Drive 連結。")
    return ui_text.trim_message("\n".join(lines))


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


class DriveLinkSubmissionModal(discord.ui.Modal):
    """大檔案 Drive 連結提交：即使原任務是檔案，也允許用連結完成。"""

    def __init__(self, cog: "SubmissionsCog", task: repositories.TaskItem, folder_url: str = ""):
        super().__init__(title=f"Drive 連結｜{task.name}"[:45])
        self.cog = cog
        self.task = task
        self.folder_url = folder_url
        self.link_input = discord.ui.TextInput(
            label="Drive 檔案或資料夾連結（必填）",
            placeholder=folder_url or "https://drive.google.com/...",
            required=True,
            max_length=500,
        )
        self.note_input = discord.ui.TextInput(
            label="補充說明（選填）",
            style=discord.TextStyle.paragraph,
            placeholder="例如：檔案已放在指定資料夾，檔名是...",
            required=False,
            max_length=1000,
        )
        self.add_item(self.link_input)
        self.add_item(self.note_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        link_url = (self.link_input.value or "").strip()
        content = (self.note_input.value or "").strip()
        if not is_valid_link(link_url):
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
        if self.folder_url and self.folder_url != link_url:
            content = "\n".join(
                part for part in [content, f"指定資料夾：{self.folder_url}"] if part
            )
        try:
            replaced = await repositories.mark_previous_submissions_not_latest(self.task.page_id)
            submission_page = await repositories.create_submission(
                self.task, artist,
                submit_type="Drive 連結", link_url=link_url, content=content,
            )
            await repositories.set_task_under_review(self.task.page_id, link_url)
        except Exception:
            logger.exception("Drive 連結提交失敗")
            await interaction.followup.send("❌ 寫入 Notion 失敗，請稍後再試或聯絡管理者。", ephemeral=True)
            return

        self.cog.pending_file_submissions.pop(interaction.user.id, None)
        await audit.log_event(
            event_name=f"提交任務（大檔連結）｜{artist.name}｜{self.task.name}",
            action_type="提交", operator=artist.name,
            discord_user_id=str(interaction.user.id),
            target_db="Submissions & File Index",
            target_url=submission_page.get("url", ""),
            after="Drive 連結｜任務轉待審核" + (f"｜取代 {replaced} 筆舊版" if replaced else ""),
        )
        internal = interaction.client.get_channel(config.ADMIN_NOTIFY_CHANNEL_ID)
        await channel_guard.try_send(
            internal,
            content=(
                f"📥 **新提交待審核（大檔 Drive 連結）**\n"
                f"任務：{self.task.name}｜{artist.name}\n"
                f"Drive：{link_url}\nNotion：{submission_page.get('url', '')}"
            ),
        )
        resubmit_note = f"\n（已取代先前 {replaced} 筆提交，這筆為最新版）" if replaced else ""
        await interaction.followup.send(
            f"✅ 已提交 Drive 連結：**{self.task.name}**\n"
            f"任務狀態已轉「待審核」，審核結果會再通知你。{resubmit_note}",
            ephemeral=True,
        )


class ReviewFeedbackDriveLinkModal(discord.ui.Modal):
    """老師大批改檔 Drive 連結補回饋。"""

    def __init__(
        self,
        cog: "SubmissionsCog",
        submission: repositories.SubmissionItem,
        *,
        result: str,
        feedback: str,
        reviewer: str,
        targets: list[ReviewFeedbackDriveTarget],
    ):
        super().__init__(title=f"批改檔連結｜{submission.name}"[:45])
        self.cog = cog
        self.submission = submission
        self.result = result
        self.feedback = feedback
        self.reviewer = reviewer
        self.targets = targets
        self.link_input = discord.ui.TextInput(
            label="Drive 批改檔或資料夾連結（必填）",
            placeholder="https://drive.google.com/...",
            required=True,
            max_length=500,
        )
        self.note_input = discord.ui.TextInput(
            label="補充回饋（選填）",
            style=discord.TextStyle.paragraph,
            placeholder="例如：批改檔已放在 Johnny 老師回饋資料夾",
            required=False,
            max_length=1000,
        )
        self.add_item(self.link_input)
        self.add_item(self.note_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        link_url = (self.link_input.value or "").strip()
        note = (self.note_input.value or "").strip()
        if not is_valid_link(link_url):
            await interaction.followup.send(
                "❌ 連結格式看起來不對（要以 http:// 或 https:// 開頭），請確認後重新提交。",
                ephemeral=True,
            )
            return

        folder_lines = [f"{target.artist_name}：{target.folder_url}" for target in self.targets]
        feedback_parts = [self.feedback, note]
        if folder_lines:
            feedback_parts.append("老師回饋資料夾：\n" + "\n".join(folder_lines))
        enhanced_feedback = feedback_with_attachment_links(
            "\n".join(part for part in feedback_parts if part),
            [link_url],
        )
        now = datetime.now(config.get_tenant().tz)
        try:
            await repositories.update_submission_review(
                self.submission.page_id,
                status=self.result,
                feedback=enhanced_feedback,
                reviewer=self.reviewer,
                source="Discord",
                notified_at=now,
            )
        except NotionAPIError:
            logger.exception("批改檔 Drive 連結寫入 Notion 失敗")
            await interaction.followup.send("❌ 批改檔連結寫入 Notion 失敗，請稍後再試。", ephemeral=True)
            return

        self.cog.pending_review_feedback_files.pop(interaction.user.id, None)
        links_by_artist = {
            page_id: [link_url]
            for page_id in self.submission.artist_page_ids
        }
        sent = await self.cog._notify_review_feedback_file(
            self.submission,
            result=self.result,
            feedback=self.feedback or note,
            reviewer=self.reviewer,
            links_by_artist_page_id=links_by_artist,
        )
        await audit.log_event(
            event_name=f"審核批改檔連結｜{self.submission.name}",
            action_type="提交",
            operator=self.reviewer,
            discord_user_id=str(interaction.user.id),
            target_db="Submissions & File Index",
            target_url=self.submission.url,
            after=f"{link_url}｜通知 {sent} 位藝人",
        )
        await interaction.followup.send(
            f"✅ 批改檔連結已補到 Notion 並通知藝人：**{self.submission.name}**\n"
            f"通知：{sent} 位",
            ephemeral=True,
        )


async def _archive_file_submission_batch(
    *,
    client: discord.Client,
    task: repositories.TaskItem,
    artist: repositories.Artist,
    attachments: list[discord.Attachment],
    discord_user_id: int,
    base_folder_id: str = "",
    folder_path: list[str] | None = None,
    folder_url: str = "",
) -> tuple[bool, str]:
    """多檔案提交核心：整批上傳到同一提交資料夾，再寫一筆 Notion。"""
    if not attachments:
        return False, "❌ 沒有收到檔案，請重新執行 `/提交任務`。"
    for attachment in attachments:
        size_error = direct_upload_size_error(attachment.size)
        if size_error:
            return False, size_error
    now = datetime.now(config.get_tenant().tz)
    if not base_folder_id:
        target = drive_archive.resolve_archive_target(task, artist.drive_folder_url, now)
        if target is None:
            return False, (
                "❌ 你的藝人專區資料夾還沒設定（Notion Artists 的「Google Drive 資料夾」），\n"
                "請聯絡營運；或改用 `/提交任務` 貼雲端連結。"
            )
        base_folder_id, task_path = target
        folder_path = [*task_path, submission_folder_name(now)]
    folder_path = folder_path or [submission_folder_name(now)]
    if not folder_url:
        try:
            folder = await drive_api.ensure_folder_path(
                base_folder_id=base_folder_id,
                path_segments=folder_path,
            )
            folder_url = folder.get("webViewLink", "")
        except Exception:
            logger.exception("建立提交資料夾失敗")
            return False, "❌ 無法建立提交資料夾，請稍後再試或改用 Drive 連結提交。"

    uploaded_files: list[dict] = []
    try:
        for attachment in attachments:
            content = await attachment.read()
            drive_filename = prefixed_submission_filename(
                artist_name=artist.name,
                task_name=task.name,
                original_filename=attachment.filename,
            )
            uploaded = await drive_api.upload_submission_file(
                base_folder_id=base_folder_id,
                path_segments=folder_path,
                filename=drive_filename,
                content=content,
                mime_type=attachment.content_type or "",
            )
            uploaded_files.append({
                "drive_filename": drive_filename,
                "original_filename": attachment.filename,
                "size_mb": round(attachment.size / 1048576, 2),
                "resource": uploaded,
            })
    except Exception:
        logger.exception("Drive 上傳失敗")
        return False, (
            "❌ 檔案上傳 Google Drive 失敗（未寫入任何紀錄）。\n"
            "請稍後再試，或改用 `/提交任務` 貼雲端連結。"
        )

    file_size_mb = round(sum(item["size_mb"] for item in uploaded_files), 2)
    file_names = "、".join(item["original_filename"] for item in uploaded_files)
    file_ids = "、".join(item["resource"].get("id", "") for item in uploaded_files if item["resource"].get("id"))
    file_summary = f"本次提交共 {len(uploaded_files)} 個檔案：{file_names}"
    try:
        replaced = await repositories.mark_previous_submissions_not_latest(task.page_id)
        submission_page = await repositories.create_submission(
            task, artist,
            submit_type="檔案",
            link_url=folder_url,
            content=file_summary,
            drive_file_id=file_ids,
            original_filename=file_names,
            file_size_mb=file_size_mb,
        )
        await repositories.set_task_under_review(task.page_id, folder_url)
    except Exception:
        logger.exception("檔案提交寫入 Notion 失敗")
        return False, f"⚠️ 檔案已上傳到 Drive（{folder_url}），但 Notion 紀錄寫入失敗，請聯絡營運補登。"

    folder_text = "／".join(folder_path)
    display_files = "、".join(item["drive_filename"] for item in uploaded_files)
    await audit.log_event(
        event_name=f"提交任務（檔案）｜{artist.name}｜{task.name}",
        action_type="上傳", operator=artist.name,
        discord_user_id=str(discord_user_id),
        target_db="Submissions & File Index",
        target_url=submission_page.get("url", ""),
        after=f"{display_files}｜{file_size_mb}MB｜{folder_text}"
              + (f"｜取代 {replaced} 筆舊版" if replaced else ""),
    )
    internal = client.get_channel(config.ADMIN_NOTIFY_CHANNEL_ID)
    await channel_guard.try_send(
        internal,
        content=(
            f"📥 **新提交待審核（檔案）**\n"
            f"任務：{task.name}｜{artist.name}｜{len(uploaded_files)} 個檔案（{file_size_mb}MB）\n"
            f"Drive：{folder_url}\nNotion：{submission_page.get('url', '')}"
        ),
    )
    resubmit_note = f"\n（已取代先前 {replaced} 筆提交，這筆為最新版）" if replaced else ""
    return True, (
        f"✅ 檔案已提交並歸檔：**{task.name}**\n"
        f"📁 {artist.name}專區／{folder_text}\n"
        f"檔案數：{len(uploaded_files)}｜{display_files}\n"
        f"任務狀態已轉「待審核」，審核結果會再通知你。{resubmit_note}"
    )


async def _archive_file_submission(
    *,
    client: discord.Client,
    task: repositories.TaskItem,
    artist: repositories.Artist,
    attachment: discord.Attachment,
    discord_user_id: int,
) -> tuple[bool, str]:
    """單檔舊入口包裝；仍寫入新的提交資料夾。"""
    return await _archive_file_submission_batch(
        client=client,
        task=task,
        artist=artist,
        attachments=[attachment],
        discord_user_id=discord_user_id,
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
    _ok, message = await _archive_file_submission(
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
                description=submit_task_option_description(t, now),
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
        if task.submit_method == "Drive 連結":
            await self.cog.start_file_upload_wait(interaction, task, drive_only=True)
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


class LargeFileSubmissionChoiceView(discord.ui.View):
    def __init__(
        self,
        cog: "SubmissionsCog",
        task: repositories.TaskItem,
        folder_url: str = "",
        target: tuple[str, list[str]] | None = None,
        owner_id: int | None = None,
        drive_only: bool = False,
    ):
        super().__init__(timeout=FILE_UPLOAD_WAIT_MINUTES * 60)
        self.cog = cog
        self.task = task
        self.folder_url = folder_url
        self.target = target
        self.owner_id = owner_id
        if drive_only:
            for child in self.children:
                if isinstance(child, discord.ui.Button) and child.label == "完成提交":
                    child.disabled = True

    @discord.ui.button(label="貼 Drive 連結完成提交", style=discord.ButtonStyle.primary)
    async def submit_drive_link(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DriveLinkSubmissionModal(self.cog, self.task, self.folder_url))

    @discord.ui.button(label="我已上傳到 Drive", style=discord.ButtonStyle.success)
    async def verify_drive_upload(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.owner_id is not None and interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ 這個提交入口只給原本操作的人使用。", ephemeral=True)
            return
        await self.cog.complete_drive_folder_submission(interaction)

    @discord.ui.button(label="完成提交", style=discord.ButtonStyle.success)
    async def finish_submission(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.owner_id is not None and interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ 這個提交入口只給原本操作的人使用。", ephemeral=True)
            return
        pending = self.cog.pending_file_submissions.get(interaction.user.id)
        if pending is None or not pending.attachments:
            await interaction.response.send_message("請先上傳至少一個檔案，再按「完成提交」。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        self.cog.pending_file_submissions.pop(interaction.user.id, None)
        await interaction.followup.send("收到全部檔案，正在歸檔到 Google Drive 並寫入 Notion...", ephemeral=True)
        _ok, result_message = await _archive_file_submission_batch(
            client=interaction.client,
            task=pending.task,
            artist=pending.artist,
            attachments=pending.attachments,
            discord_user_id=interaction.user.id,
            base_folder_id=pending.folder_base_id,
            folder_path=pending.folder_path,
            folder_url=pending.folder_url,
        )
        await interaction.followup.send(result_message, ephemeral=True)


class ReviewFeedbackLargeFileChoiceView(discord.ui.View):
    def __init__(
        self,
        cog: "SubmissionsCog",
        submission: repositories.SubmissionItem,
        *,
        result: str,
        feedback: str,
        reviewer: str,
        targets: list[ReviewFeedbackDriveTarget],
        owner_id: int | None = None,
    ):
        super().__init__(timeout=FILE_UPLOAD_WAIT_MINUTES * 60)
        self.cog = cog
        self.submission = submission
        self.result = result
        self.feedback = feedback
        self.reviewer = reviewer
        self.targets = targets
        self.owner_id = owner_id

    @discord.ui.button(label="貼 Drive 連結補回饋", style=discord.ButtonStyle.primary)
    async def submit_drive_link(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            ReviewFeedbackDriveLinkModal(
                self.cog,
                self.submission,
                result=self.result,
                feedback=self.feedback,
                reviewer=self.reviewer,
                targets=self.targets,
            )
        )

    @discord.ui.button(label="完成回饋", style=discord.ButtonStyle.success)
    async def finish_feedback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.owner_id is not None and interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ 這個回饋入口只給原本審核的人使用。", ephemeral=True)
            return
        pending = self.cog.pending_review_feedback_files.get(interaction.user.id)
        if pending is None or not pending.attachments:
            await interaction.response.send_message("請先上傳至少一個批改檔，再按「完成回饋」。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        self.cog.pending_review_feedback_files.pop(interaction.user.id, None)
        await interaction.followup.send("收到全部批改檔，正在歸檔到 Google Drive、更新 Notion 並通知藝人...", ephemeral=True)
        _ok, result_message = await self.cog._archive_review_feedback_files(
            pending=pending,
            discord_user_id=interaction.user.id,
        )
        await interaction.followup.send(result_message, ephemeral=True)


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
            review_submission_detail_text(submission),
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


class PostReviewAttachmentView(discord.ui.View):
    def __init__(
        self,
        cog: "SubmissionsCog",
        submission: repositories.SubmissionItem,
        *,
        result: str,
        feedback: str,
        reviewer: str,
        reviewer_id: int,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.submission = submission
        self.result = result
        self.feedback = feedback
        self.reviewer = reviewer
        self.reviewer_id = reviewer_id

    @discord.ui.button(label="附批改檔", style=discord.ButtonStyle.secondary)
    async def attach_feedback_file(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.reviewer_id:
            await interaction.response.send_message("❌ 這個按鈕只給原本審核的人使用。", ephemeral=True)
            return
        await self.cog.start_review_feedback_file_wait(
            interaction,
            self.submission,
            result=self.result,
            feedback=self.feedback,
            reviewer=self.reviewer,
        )


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
        self.pending_review_feedback_files: dict[int, PendingReviewFeedbackFile] = {}
        self._review_notified: set[str] = set()
        self.review_poll_loop.start()

    def cog_unload(self):
        self.review_poll_loop.cancel()

    async def start_file_upload_wait(
        self,
        interaction: discord.Interaction,
        task: repositories.TaskItem,
        *,
        drive_only: bool = False,
    ) -> None:
        """選到檔案任務後，在目前頻道等待附件，避免要求藝人跳出或重打指令。"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        artist = await repositories.find_artist_by_discord_id(interaction.user.id)
        if artist is None:
            await interaction.followup.send(
                "❌ 找不到你的藝人資料，請聯絡管理者確認 Notion 上的 Discord User ID 設定。",
                ephemeral=True,
            )
            return
        now = datetime.now(config.get_tenant().tz)
        expires_at = now + timedelta(minutes=FILE_UPLOAD_WAIT_MINUTES)
        folder_url = ""
        folder_base_id = ""
        folder_path: list[str] = []
        target = drive_archive.resolve_archive_target(task, artist.drive_folder_url, now)
        if target is not None and drive_api.is_configured():
            base_folder_id, path_segments = target
            folder_base_id = base_folder_id
            folder_path = [*path_segments, submission_folder_name(now)]
            try:
                folder = await drive_api.ensure_folder_path(
                    base_folder_id=folder_base_id,
                    path_segments=folder_path,
                )
                folder_url = folder.get("webViewLink", "")
            except Exception:
                logger.exception("產生大檔 Drive 目標資料夾失敗")
        self.pending_file_submissions[interaction.user.id] = PendingFileSubmission(
            task=task,
            artist=artist,
            channel_id=interaction.channel_id,
            created_at=now,
            expires_at=expires_at,
            folder_base_id=folder_base_id,
            folder_path=folder_path,
            folder_url=folder_url,
        )

        upload_intro = (
            f"📁 請先把 **{task.name}** 的檔案上傳到指定 Google Drive 資料夾，"
            "完成後按「我已上傳到 Drive」。\n"
            if drive_only
            else f"📎 請直接在這個頻道上傳 **{task.name}** 的檔案。\n"
        )
        upload_controls = (
            "上傳完成後按「我已上傳到 Drive」；若驗證失敗，也可改按「貼 Drive 連結完成提交」。\n"
            if drive_only
            else "你可以連續上傳多個檔案；全部上傳後按「完成提交」。\n"
        )
        await interaction.followup.send(
            upload_intro
            + upload_controls
            + f"每個檔案請在 **{MAX_UPLOAD_MB}MB 以下**；入口 **{file_upload_wait_label()}**後失效。\n\n"
            + large_file_drive_instruction(task_name=task.name, folder_url=folder_url),
            view=LargeFileSubmissionChoiceView(
                self, task, folder_url=folder_url, target=target,
                owner_id=interaction.user.id, drive_only=drive_only,
            ),
            ephemeral=True,
        )

    async def complete_drive_folder_submission(self, interaction: discord.Interaction) -> None:
        """驗證指定資料夾的新檔案，成功後建立一筆檔案提交。"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        pending = self.pending_file_submissions.get(interaction.user.id)
        if pending is None or not pending.folder_url or not pending.folder_base_id:
            await interaction.followup.send(
                "❌ 目前沒有可驗證的指定資料夾，請重新執行 `/提交任務` 或改貼 Drive 連結。",
                ephemeral=True,
            )
            return
        folder_id = drive_archive.extract_drive_folder_id(pending.folder_url)
        if not folder_id:
            await interaction.followup.send("❌ 找不到指定 Drive 資料夾，請改貼 Drive 連結提交。", ephemeral=True)
            return
        try:
            files = await drive_api.list_recent_files(folder_id, pending.created_at)
        except Exception:
            logger.exception("驗證 Drive 資料夾檔案失敗")
            await interaction.followup.send("❌ 讀取 Drive 資料夾失敗，請稍後再按一次。", ephemeral=True)
            return
        if not files:
            await interaction.followup.send(
                "⚠️ 目前找不到入口建立後新增的檔案，請確認檔案已上傳到指定資料夾後再試。",
                ephemeral=True,
            )
            return
        names, file_ids, total_mb = drive_folder_submission_summary(files)
        file_links = "、".join(
            str(item["webViewLink"])
            for item in files
            if item.get("webViewLink")
        )
        content = f"Drive 資料夾驗證完成，共 {len(files)} 個檔案：{names}"
        if file_links:
            content += f"\n檔案連結：{file_links}"
        try:
            await repositories.mark_previous_submissions_not_latest(pending.task.page_id)
            submission_page = await repositories.create_submission(
                pending.task,
                pending.artist,
                submit_type="檔案",
                link_url=pending.folder_url,
                content=content,
                drive_file_id=file_ids,
                original_filename=names,
                file_size_mb=total_mb,
            )
            await repositories.set_task_under_review(pending.task.page_id, pending.folder_url)
        except Exception:
            logger.exception("Drive 資料夾提交寫入 Notion 失敗")
            await interaction.followup.send("❌ Drive 已讀到檔案，但 Notion 寫入失敗，請聯絡控方。", ephemeral=True)
            return
        self.pending_file_submissions.pop(interaction.user.id, None)
        await audit.log_event(
            event_name=f"提交任務（Drive 資料夾）｜{pending.artist.name}｜{pending.task.name}",
            action_type="上傳",
            operator=pending.artist.name,
            discord_user_id=str(interaction.user.id),
            target_db="Submissions & File Index",
            target_url=submission_page.get("url", ""),
            after=f"{len(files)} 個檔案｜{total_mb}MB｜{pending.folder_url}",
        )
        internal = interaction.client.get_channel(config.ADMIN_NOTIFY_CHANNEL_ID)
        await channel_guard.try_send(
            internal,
            content=(
                f"📥 **新提交待審核（Drive 資料夾）**\n"
                f"任務：{pending.task.name}｜{pending.artist.name}｜{len(files)} 個檔案（{total_mb}MB）\n"
                f"Drive：{pending.folder_url}\nNotion：{submission_page.get('url', '')}"
            ),
        )
        await interaction.followup.send(
            f"✅ 已驗證 Drive 資料夾並提交：**{pending.task.name}**\n"
            f"檔案數：{len(files)}｜{names}\n"
            f"任務狀態已轉「待審核」。",
            ephemeral=True,
        )

    async def start_review_feedback_file_wait(
        self,
        interaction: discord.Interaction,
        submission: repositories.SubmissionItem,
        *,
        result: str,
        feedback: str,
        reviewer: str,
    ) -> None:
        """審核後在同一頻道等待老師批改檔，避免要求控方跳私訊或重跑流程。"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        now = datetime.now(config.get_tenant().tz)
        expires_at = now + timedelta(minutes=FILE_UPLOAD_WAIT_MINUTES)
        self.pending_review_feedback_files[interaction.user.id] = PendingReviewFeedbackFile(
            submission=submission,
            result=result,
            feedback=feedback,
            reviewer=reviewer,
            channel_id=interaction.channel_id,
            created_at=now,
            expires_at=expires_at,
        )
        targets: list[ReviewFeedbackDriveTarget] = []
        skipped: list[str] = []
        if drive_api.is_configured():
            targets, skipped = await self._review_feedback_drive_targets(submission, now)
        self.pending_review_feedback_files[interaction.user.id].targets = targets

        await interaction.followup.send(
            f"📎 請直接在這個頻道上傳 **{submission.name}** 的批改檔。\n"
            f"你可以連續上傳多個批改檔，全部上傳後按「完成回饋」。\n"
            f"每個檔案請在 **{MAX_UPLOAD_MB}MB 以下**；入口 **{file_upload_wait_label()}**後失效。\n\n"
            + review_feedback_drive_instruction(
                submission_name=submission.name,
                targets=targets,
                skipped_artists=skipped,
            ),
            view=ReviewFeedbackLargeFileChoiceView(
                self,
                submission,
                result=result,
                feedback=feedback,
                reviewer=reviewer,
                targets=targets,
                owner_id=interaction.user.id,
            ),
            ephemeral=True,
        )

    async def _review_feedback_drive_targets(
        self,
        submission: repositories.SubmissionItem,
        now: datetime,
    ) -> tuple[list[ReviewFeedbackDriveTarget], list[str]]:
        task = await repositories.get_task(submission.task_page_ids[0]) if submission.task_page_ids else None
        if task is None:
            return [], ["找不到關聯任務"]
        artist_map = await repositories.get_artist_map()
        targets: list[ReviewFeedbackDriveTarget] = []
        skipped: list[str] = []
        for artist_page_id in submission.artist_page_ids:
            artist = artist_map.get(artist_page_id)
            if not artist:
                continue
            target = drive_archive.resolve_review_feedback_target(artist.drive_folder_url, now, task)
            if target is None:
                skipped.append(artist.name)
                continue
            base_folder_id, path_segments = target
            path_segments = [*path_segments, drive_archive.review_feedback_folder_name(now)]
            try:
                folder = await drive_api.ensure_folder_path(
                    base_folder_id=base_folder_id,
                    path_segments=path_segments,
                )
            except Exception:
                logger.exception("產生老師回饋 Drive 目標資料夾失敗（artist=%s）", artist.name)
                skipped.append(artist.name)
                continue
            folder_url = folder.get("webViewLink", "")
            if not folder_url:
                skipped.append(artist.name)
                continue
            targets.append(
                ReviewFeedbackDriveTarget(
                    artist_page_id=artist.page_id,
                    artist_name=artist.name,
                    folder_url=folder_url,
                    base_folder_id=base_folder_id,
                    path_segments=path_segments,
                )
            )
        return targets, skipped

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """所有 pending 附件流程的統一入口；例外全部攔在這裡，不讓使用者的檔案憑空消失。"""
        if message.author.bot:
            return
        try:
            await self._dispatch_pending_message(message)
        except Exception:
            logger.exception("提交/審核附件訊息處理失敗")
            try:
                await message.channel.send("⚠️ 處理你剛剛的訊息時發生錯誤，請重新執行指令再試一次。")
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("提交/審核錯誤通知也發送失敗")

    async def _dispatch_pending_message(self, message: discord.Message) -> None:
        pending_feedback = self.pending_review_feedback_files.get(message.author.id)
        if pending_feedback is not None:
            await self._handle_pending_review_feedback_message(message, pending_feedback)
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
        if not drive_api.is_configured():
            await message.channel.send("❌ Drive 歸檔尚未設定，請先改用 Drive 連結提交。")
            return
        for attachment in message.attachments:
            size_error = direct_upload_size_error(attachment.size)
            if size_error:
                await message.channel.send(
                    f"❌ `{attachment.filename}`：{size_error}\n"
                    "請回到剛才的操作訊息，按「我已上傳到 Drive」完成驗證。"
                )
                continue
            pending.attachments.append(attachment)
        count = len(pending.attachments)
        await message.channel.send(
            f"✅ 已收到 {len(message.attachments)} 個檔案，目前共 {count} 個。"
            "可以繼續上傳；全部完成後按原本訊息的「完成提交」。"
        )

    async def _handle_pending_review_feedback_message(
        self,
        message: discord.Message,
        pending: PendingReviewFeedbackFile,
    ) -> None:
        if pending.channel_id is not None and message.channel.id != pending.channel_id:
            return
        now = datetime.now(config.get_tenant().tz)
        if pending.is_expired(now):
            self.pending_review_feedback_files.pop(message.author.id, None)
            await message.channel.send("❌ 這次批改檔上傳入口已過期，請重新執行 `/審核提交`。")
            return
        if not message.attachments:
            await message.channel.send(
                "我目前沒有讀到附件。請直接上傳一個批改檔附件；如果你剛剛已經上傳但我仍讀不到，"
                "代表 Discord Bot 的 Message Content Intent 尚未開啟，請通知控方處理。"
            )
            return
        if not drive_api.is_configured():
            await message.channel.send("❌ Drive 歸檔尚未設定，請先把批改檔上傳到 Drive，再補連結到 Notion。")
            return
        for attachment in message.attachments:
            size_error = direct_upload_size_error(attachment.size)
            if size_error:
                await message.channel.send(
                    f"`{attachment.filename}`：{size_error}\n"
                    "批改檔超過標準大小時，請先上傳到 Drive，再把連結補到 Notion 的審核回饋。"
                )
                continue
            pending.attachments.append(attachment)
        await message.channel.send(
            f"✅ 已收到 {len(message.attachments)} 個批改檔，目前共 {len(pending.attachments)} 個。"
            "可以繼續上傳；全部完成後按原本訊息的「完成回饋」。"
        )

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

    async def _notify_review_feedback_file(
        self,
        submission: repositories.SubmissionItem,
        *,
        result: str,
        feedback: str,
        reviewer: str,
        links_by_artist_page_id: dict[str, list[str]],
    ) -> int:
        artist_map = await repositories.get_artist_map()
        sent = 0
        for artist_page_id in submission.artist_page_ids:
            artist = artist_map.get(artist_page_id)
            if not artist or not artist.discord_user_id:
                continue
            links = links_by_artist_page_id.get(artist_page_id, [])
            if not links:
                continue
            try:
                user = await self.bot.fetch_user(int(artist.discord_user_id))
                await user.send(
                    review_feedback_file_dm_text(
                        submission,
                        result=result,
                        feedback=feedback,
                        reviewer=reviewer,
                        file_links=links,
                    )
                )
                sent += 1
            except Exception:
                logger.exception("批改檔 DM 失敗（submission=%s, artist=%s）", submission.page_id, artist.name)
        return sent

    async def _archive_review_feedback_files(
        self,
        *,
        pending: PendingReviewFeedbackFile,
        discord_user_id: int,
    ) -> tuple[bool, str]:
        if not pending.attachments:
            return False, "❌ 沒有收到批改檔，請重新執行 `/審核提交`。"
        for attachment in pending.attachments:
            size_error = direct_upload_size_error(attachment.size)
            if size_error:
                return False, size_error
        now = datetime.now(config.get_tenant().tz)
        links_by_artist: dict[str, list[str]] = {}
        flat_links: list[str] = []
        if not pending.targets:
            return False, "❌ 找不到可用的老師回饋資料夾，請先確認藝人 Drive 設定。"
        folder_lines = [f"{target.artist_name}：{target.folder_url}" for target in pending.targets]
        for target in pending.targets:
            for attachment in pending.attachments:
                try:
                    content = await attachment.read()
                    drive_filename = prefixed_review_feedback_filename(
                        submission_name=pending.submission.name,
                        original_filename=attachment.filename,
                    )
                    uploaded = await drive_api.upload_submission_file(
                        base_folder_id=target.base_folder_id,
                        path_segments=target.path_segments,
                        filename=drive_filename,
                        content=content,
                        mime_type=attachment.content_type or "",
                    )
                except Exception:
                    logger.exception("批改檔 Drive 上傳失敗（artist=%s）", target.artist_name)
                    return False, (
                        "❌ 批改檔上傳 Google Drive 失敗（未更新 Notion）。\n"
                        "請稍後再試，或先上傳到 Drive 後把連結補到 Notion 審核回饋。"
                    )
                drive_url = uploaded.get("webViewLink", "")
                if drive_url:
                    links_by_artist.setdefault(target.artist_page_id, []).append(drive_url)
                    flat_links.append(drive_url)
        if not flat_links:
            return False, (
                "❌ 找不到可用的藝人專區資料夾，批改檔未上傳。\n"
                "請確認 Artists 的「Google Drive 資料夾」，或先把檔案上傳到 Drive 後補連結。"
            )

        feedback_parts = [pending.feedback]
        if folder_lines:
            feedback_parts.append("老師回饋資料夾：\n" + "\n".join(folder_lines))
        enhanced_feedback = feedback_with_attachment_links(
            "\n".join(part for part in feedback_parts if part), flat_links,
        )
        try:
            await repositories.update_submission_review(
                pending.submission.page_id,
                status=pending.result,
                feedback=enhanced_feedback,
                reviewer=pending.reviewer,
                source="Discord",
                notified_at=now,
            )
        except NotionAPIError:
            logger.exception("批改檔連結寫入 Notion 失敗")
            return False, (
                f"⚠️ 批改檔已上傳到 Drive（{flat_links[0]}），但 Notion 回饋寫入失敗，請手動補登。"
            )

        sent = await self._notify_review_feedback_file(
            pending.submission,
            result=pending.result,
            feedback=pending.feedback,
            reviewer=pending.reviewer,
            links_by_artist_page_id=links_by_artist,
        )
        file_size_mb = round(sum(a.size for a in pending.attachments) / 1048576, 2)
        await audit.log_event(
            event_name=f"審核批改檔｜{pending.submission.name}",
            action_type="上傳", operator=pending.reviewer,
            discord_user_id=str(discord_user_id),
            target_db="Submissions & File Index",
            target_url=pending.submission.url,
            after=f"{len(pending.attachments)} 個檔案｜{file_size_mb}MB｜通知 {sent} 位藝人",
        )
        return True, (
            f"✅ {len(pending.attachments)} 個批改檔已歸檔並通知藝人：**{pending.submission.name}**\n"
            f"通知：{sent} 位藝人"
        )

    async def _archive_review_feedback_file(
        self,
        *,
        pending: PendingReviewFeedbackFile,
        attachment: discord.Attachment,
        discord_user_id: int,
    ) -> tuple[bool, str]:
        """單檔舊入口包裝；仍寫入新的本次回饋資料夾。"""
        pending.attachments = [attachment]
        return await self._archive_review_feedback_files(
            pending=pending,
            discord_user_id=discord_user_id,
        )

    async def _update_review_task_status(self, submission: repositories.SubmissionItem, result: str) -> None:
        task_status = review_task_status_for_result(result)
        if not task_status:
            return
        for task_page_id in submission.task_page_ids:
            await repositories.update_task_status(task_page_id, task_status)
            # 修改期限以 48 小時為預設；控方仍可直接在 Notion 調整日期。
            deadline = (
                datetime.now(config.get_tenant().tz) + timedelta(hours=48)
                if result in ("需修改", "退回")
                else None
            )
            await repositories.set_task_revision_deadline(task_page_id, deadline)

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
            f"已通知藝人：{sent} 位\n"
            "若要補批改檔，可按下方按鈕後直接在這個頻道上傳附件。",
            view=PostReviewAttachmentView(
                self,
                submission,
                result=result,
                feedback=feedback,
                reviewer=reviewer,
                reviewer_id=interaction.user.id,
            ),
            ephemeral=True,
        )

    def _sweep_expired_pending(self) -> None:
        """清掉逾時未完成的上傳/回饋入口，避免使用者中途放棄後 dict 一直長大。"""
        now = datetime.now(config.get_tenant().tz)
        for pending_by_user in (
            self.pending_file_submissions,
            self.pending_review_feedback_files,
        ):
            for key in expired_pending_keys(pending_by_user, now):
                pending_by_user.pop(key, None)

    @tasks.loop(minutes=1)
    async def review_poll_loop(self):
        self._sweep_expired_pending()
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
    async def submit_task(
        self,
        interaction: discord.Interaction,
    ):
        if not await channel_guard.ensure_task_channel(interaction):
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
        await interaction.followup.send(
            submit_task_intro_text(artist.name, submittable, now),
            view=SubmitTaskView(self, submittable, now),
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
