"""P0-3 作業提交：由 /我的任務 進入 Drive 提交與審核流程。"""
import json
import logging
import re
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from cogs import channel_guard, ui_text
from services.permissions import is_admin
from services import audit, drive_api, drive_archive, notification_log, ops_notice, repositories, ritsu_voice, task_notifications
from services.notion_api import NotionAPIError

logger = logging.getLogger(__name__)

LINK_PREFIXES = ("http://", "https://")
FREE_DISCORD_UPLOAD_MB = 10
MAX_UPLOAD_MB = FREE_DISCORD_UPLOAD_MB  # 標準流程假設藝人使用 Discord 免費方案
def _file_upload_wait_minutes() -> int:
    """Drive 上傳等待時間；租戶可用 FILE_UPLOAD_WAIT_MINUTES 調整。"""
    return config.get_tenant().file_upload_wait_minutes


# 向後相容別名：既有測試與文案沿用這個名稱讀當前租戶設定
FILE_UPLOAD_WAIT_MINUTES = _file_upload_wait_minutes()
REVIEW_RESULTS = ("通過", "需修改", "退回")
FILENAME_COMPONENT_LIMIT = 60
ORIGINAL_FILENAME_LIMIT = 120
_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>\|\r\n\t]+')
_WHITESPACE_CHARS = re.compile(r"\s+")


async def notify_submission_reviewers(
    client: discord.Client, task: repositories.TaskItem, message: str,
) -> None:
    """新提交待審核的通知對象（M4-15）：`審核人員` 有填就私訊那些人，沒填才退回控方頻道。

    不是「兩邊都發」——跟 cogs/reminders.py 的待審核催辦（_check_review_reminders）不同，
    那支會同時通知指定審核人員與控方頻道；這支是單純 fallback，有人接手就不吵控方頻道。
    不寫死人名，符合多租戶平展化原則；`審核人員` 填了人但那個人缺 Discord ID，
    視同沒有可通知對象，一樣退回控方頻道，不會讓提交默默沒人知道。
    """
    message = f"{message}\n\n{task_notifications.REVIEW_HOW_TO}"
    reviewers = await repositories.get_people_by_ids(task.reviewer_page_ids)
    notifiable = [r for r in reviewers if r.discord_user_id]
    if not notifiable:
        from cogs.ops_actions import ops_action_view

        # 頻道裡的版本是「要處理」：沒有審核人，要有人認領（M3-25）
        lines = message.splitlines()
        title = lines[0].replace("**", "").replace("📥 ", "").strip() if lines else "新提交待審核"
        body = "\n".join(lines[1:])
        await channel_guard.try_send(
            channel_guard.ops_channel(client),
            content=ops_notice.action_title(f"{title}（沒有指定審核人）") + ("\n" + body if body else ""),
            view=ops_action_view(),
        )
        return
    for reviewer in notifiable:
        try:
            user = await client.fetch_user(int(reviewer.discord_user_id))
            await user.send(message)
        except Exception:
            logger.exception("審核人員通知失敗：%s", reviewer.name)


# 已建好但還沒交出去的 Drive 版本資料夾：{discord_user_id: {task_page_id: {...}}}。
# 上傳入口是 ephemeral 訊息，藝人離開 Discord 去上傳、回來就不見了，只能重新進入；
# 重新進入同一筆任務要回到同一個 ver 資料夾，否則已上傳的檔案會被晾在舊夾、新夾驗證不到。
# 存檔而不是只放記憶體，bot 重啟後一樣找得回來。
DRIVE_UPLOAD_STATE_PATH = Path(".state/pending_drive_uploads.json")


def load_drive_upload_state(path: Path | None = None) -> dict:
    try:
        raw = json.loads((path or DRIVE_UPLOAD_STATE_PATH).read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def save_drive_upload_state(state: dict, path: Path | None = None) -> None:
    path = path or DRIVE_UPLOAD_STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def reusable_drive_upload(
    state: dict, user_id: int, task_page_id: str, now: datetime,
) -> dict | None:
    """同一人、同一任務、入口未過期 → 回傳上次的資料夾（created_at 沿用，才驗證得到先前上傳的檔）。"""
    tasks_by_id = state.get(str(user_id))
    entry = tasks_by_id.get(task_page_id) if isinstance(tasks_by_id, dict) else None
    if not isinstance(entry, dict) or not entry.get("folder_url") or not entry.get("folder_base_id"):
        return None
    try:
        created_at = datetime.fromisoformat(entry["created_at"])
        expires_at = datetime.fromisoformat(entry["expires_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if now > expires_at:
        return None
    return {
        "created_at": created_at,
        "folder_base_id": entry["folder_base_id"],
        "folder_path": list(entry.get("folder_path") or []),
        "folder_url": entry["folder_url"],
    }


def remember_drive_upload(
    state: dict, user_id: int, task_page_id: str, pending: "PendingFileSubmission", now: datetime,
) -> dict:
    """記下這次的資料夾，順手清掉已過期的紀錄。"""
    cleaned: dict = {}
    for uid, tasks_by_id in state.items():
        if not isinstance(tasks_by_id, dict):
            continue
        kept = {
            tid: entry for tid, entry in tasks_by_id.items()
            if isinstance(entry, dict) and _entry_not_expired(entry, now)
        }
        if kept:
            cleaned[uid] = kept
    cleaned.setdefault(str(user_id), {})[task_page_id] = {
        "created_at": pending.created_at.isoformat(),
        "expires_at": pending.expires_at.isoformat(),
        "folder_base_id": pending.folder_base_id,
        "folder_path": pending.folder_path,
        "folder_url": pending.folder_url,
    }
    return cleaned


def forget_drive_upload(state: dict, user_id: int, task_page_id: str) -> dict:
    """提交成功後移除；下次再交同一任務才會開新的 ver。"""
    tasks_by_id = state.get(str(user_id))
    if isinstance(tasks_by_id, dict):
        tasks_by_id.pop(task_page_id, None)
        if not tasks_by_id:
            state.pop(str(user_id), None)
    return state


def _entry_not_expired(entry: dict, now: datetime) -> bool:
    try:
        return now <= datetime.fromisoformat(entry["expires_at"])
    except (KeyError, TypeError, ValueError):
        return False


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


def disable_all_view_items(view) -> None:
    """完成一次性操作後停用整組元件，避免使用者再走互斥分支。"""
    for item in view.children:
        if hasattr(item, "disabled"):
            item.disabled = True


def parse_revision_days(value: str) -> int | None:
    """審核人設定修改期限：只接受 1～7 個完整天數。"""
    try:
        days = int(value.strip())
    except (AttributeError, TypeError, ValueError):
        return None
    return days if 1 <= days <= 7 else None


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
        "請改上傳到 Bot 已建立的指定 Google Drive 資料夾。"
    )


def file_upload_wait_label(minutes: int | None = None) -> str:
    minutes = _file_upload_wait_minutes() if minutes is None else minutes
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


def submission_folder_name(task_name: str, created_at: datetime, version: int = 1) -> str:
    """Compatibility helper; live Drive creation resolves the next version dynamically."""
    return f"{drive_archive.versioned_folder_prefix(task_name, created_at)}{version}"


async def _ensure_next_version_folder(
    *,
    base_folder_id: str,
    parent_path: list[str],
    item_name: str,
    created_at: datetime,
) -> tuple[dict, list[str]]:
    folder = await drive_api.ensure_next_version_folder(
        base_folder_id=base_folder_id,
        parent_path_segments=parent_path,
        name_prefix=drive_archive.versioned_folder_prefix(item_name, created_at),
    )
    folder_name = str(folder.get("name") or "")
    if not folder_name:
        raise RuntimeError("Drive version folder response is missing its name")
    return folder, [*parent_path, folder_name]


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
    drive_setup_error: bool = False,
) -> str:
    lines: list[str] = []
    if folder_url:
        # M4-18 小律的口吻
        lines.append(f"📤 **「{task_name}」的資料夾準備好了！**")
        lines.append(f"把檔案上傳到這裡：{folder_url}")
        lines.append("傳好之後按「我已上傳到 Drive」，小律會去資料夾幫你確認。")
    elif drive_setup_error:
        lines.append("⚠️ 小律現在打不開公司的 Drive（授權失效或權限不足），請聯絡公司重新授權 Google Drive。")
    else:
        lines.append("⚠️ 小律這次沒辦法幫你準備資料夾，請聯絡公司檢查公司 Google Drive 設定。")
    return "\n".join(lines)


async def _resolve_submission_target(
    task: repositories.TaskItem,
    artist: repositories.Artist,
    now: datetime,
) -> tuple[tuple[str, list[str]] | None, str]:
    """Resolve an artist task archive target, creating the artist zone from the company root when needed."""
    project_name = None
    if not task.drive_folder_url:
        try:
            project_name = await repositories.task_project_name(task)
        except Exception:
            # 讀不到專案就不要猜：落到「未分專案」會把檔案放錯地方，寧可請藝人稍後再試。
            logger.exception("讀取任務專案名稱失敗，暫不建立 Drive 資料夾：%s", task.page_id)
            return None, ""
    target = drive_archive.resolve_archive_target(task, artist.drive_folder_url, now, project_name)
    if target is not None:
        base_folder_id, path_segments = target
        if path_segments and not task.drive_folder_url:
            try:
                task_folder = await drive_api.ensure_folder_path(
                    base_folder_id=base_folder_id,
                    path_segments=path_segments,
                )
                task_folder_url = str(task_folder.get("webViewLink") or "")
                # A Task has one Google Drive Folder field. Only write it when
                # the task belongs to one artist; multi-artist tasks have one
                # separate task folder under each artist's Drive zone.
                if task_folder_url and len(task.artist_page_ids) == 1:
                    try:
                        await repositories.set_task_drive_folder(
                            task.page_id, task_folder_url
                        )
                        task.drive_folder_url = task_folder_url
                    except Exception:
                        logger.exception(
                            "回寫 Task Google Drive Folder 失敗：%s", task.page_id
                        )
                elif task_folder_url:
                    try:
                        await repositories.append_task_drive_folder_link(
                            task.page_id, artist.name, task_folder_url
                        )
                    except Exception:
                        logger.exception(
                            "回寫多人 Task Drive 資料夾索引失敗：%s", task.page_id
                        )
                return target, task_folder_url
            except Exception:
                logger.exception("建立任務 Drive 資料夾失敗：%s", task.page_id)
        return target, artist.drive_folder_url
    root_id = config.get_tenant().drive_root_folder_id
    if not root_id or not drive_api.is_configured():
        return None, ""
    try:
        zone = await drive_api.ensure_folder_path(
            base_folder_id=root_id,
            path_segments=[artist.name.strip()],
        )
    except Exception:
        logger.exception("建立藝人 Drive 專區失敗：%s", artist.name)
        return None, ""
    zone_id = str(zone.get("id") or "")
    zone_url = str(zone.get("webViewLink") or "")
    if not zone_id:
        return None, ""
    task_path = drive_archive.archive_path_for_submission(task, now, project_name)
    try:
        task_folder = await drive_api.ensure_folder_path(
            base_folder_id=zone_id,
            path_segments=task_path,
        )
        task_folder_url = str(task_folder.get("webViewLink") or "")
    except Exception:
        logger.exception("建立任務 Drive 資料夾失敗：%s", task.page_id)
        task_folder_url = ""
    if task_folder_url and len(task.artist_page_ids) == 1:
        try:
            await repositories.set_task_drive_folder(task.page_id, task_folder_url)
            task.drive_folder_url = task_folder_url
        except Exception:
            logger.exception("回寫 Task Google Drive Folder 失敗：%s", task.page_id)
    elif task_folder_url:
        try:
            await repositories.append_task_drive_folder_link(
                task.page_id, artist.name, task_folder_url
            )
        except Exception:
            logger.exception("回寫多人 Task Drive 資料夾索引失敗：%s", task.page_id)
    return ((zone_id, task_path), task_folder_url or zone_url)


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
    """只給控方／老師用。藝人端一律不給 Notion 入口，見 services/notion_links。"""
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


# 這裡原本有 submission_task_link()，會在審核結果 DM 附上藝人可點的 Notion 任務連結。
# 藝人端一律不給 Notion 入口（任務頁往上翻就是整個 Tasks），因此移除；
# DM 本身已經寫明任務名稱與回饋，要看細節請用 /我的任務。


def review_dm_text(
    submission: repositories.SubmissionItem,
    *,
    task_name: str,
    result: str,
    feedback: str = "",
    reviewer: str = "",
    name: str = "",
    ritsu_line: str = "",
) -> str:
    """審核結果私訊（M4-18 小律的口吻）。ritsu_line 是小律依最近表現說的一句（AI 或範本句）。"""
    who = f"{name}同學，" if name else ""
    task = task_name or submission.name
    header = {
        "通過": f"🎉 **{who}「{task}」通過了！**",
        "需修改": f"🛠 **{who}「{task}」公司給了一些修改建議**",
        "退回": f"↩️ **{who}「{task}」這次先退回了**",
    }.get(result, f"審核結果：{result}｜**{task}**")
    lines = [header]
    if ritsu_line:
        lines.append(ritsu_line)
    if feedback:
        lines.append(f"\n**回饋**\n{feedback}")
    if reviewer:
        lines.append(f"審核：{reviewer}")
    if result in ("需修改", "退回"):
        lines.append("\n照回饋改好之後，到 `/我的任務` 按「提交任務」重新交就好，小律等你～")
    return "\n".join(lines)


def review_facts(result: str, submission: repositories.SubmissionItem, artist_page_id: str,
                 all_submissions: list[repositories.SubmissionItem], now: datetime) -> ritsu_voice.ReviewFacts:
    """給小律寫審核結果那一句用的數字：第幾次交、最近連續一次就過幾件、近 60 天通過／修改幾次。"""
    mine = [s for s in all_submissions if artist_page_id in s.artist_page_ids]
    tasks = set(submission.task_page_ids)
    attempt = max(1, sum(1 for s in mine if tasks & set(s.task_page_ids)))
    per_task: dict[str, int] = {}
    for s in mine:
        for tid in s.task_page_ids:
            per_task[tid] = per_task.get(tid, 0) + 1
    cutoff = now - timedelta(days=60)
    recent = [s for s in mine if s.created_at and s.created_at >= cutoff]
    streak = 1 if result == "通過" and attempt == 1 else 0
    if streak:
        reviewed = sorted((s for s in mine if s.page_id != submission.page_id and s.status in REVIEW_RESULTS),
                          key=lambda s: s.created_at or now, reverse=True)
        for s in reviewed:
            first_try = all(per_task.get(tid, 0) == 1 for tid in s.task_page_ids)
            if s.status != "通過" or not first_try:
                break
            streak += 1
    return ritsu_voice.ReviewFacts(
        result=result, attempt=attempt, pass_streak=streak,
        recent_passed=sum(1 for s in recent if s.status == "通過"),
        recent_fixed=sum(1 for s in recent if s.status in ("需修改", "退回")),
    )


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


def submit_task_option_description(task: repositories.TaskItem, now: datetime) -> str:
    """Select option 描述：任務類型與期限。提交一律是上傳檔案到公司 Drive（M4-03）。"""
    return f"{task.task_type or '任務'}｜{ui_text.deadline_text(task, now)}"[:100]


def _teacher_requirement_text(task: repositories.TaskItem) -> str:
    description = _clip(task_notifications._artist_facing_description(task.description), 120)
    return f"\n  任務說明：{description}" if description else ""


def submit_task_intro_text(
    artist_name: str,
    tasks: list[repositories.TaskItem],
    now: datetime,
) -> str:
    """提交第一關文案：先讓藝人知道有哪些任務。"""
    lines = [
        f"📤 {artist_name} 可提交的任務（{len(tasks)}）：",
        "先選任務；選定後 Bot 會建立你的任務 Drive 資料夾，請把檔案上傳到那裡。",
        "",
    ]
    task_lines = [
        f"・{task.name}｜{task.task_type or '任務'}｜{ui_text.deadline_text(task, now)}"
        f"{_teacher_requirement_text(task)}"
        for task in tasks
    ]
    # 任務說明可能很長，固定筆數仍會超過 Discord 上限；改成看實際長度放得下幾筆，
    # 放不下的用下方選單查看（選單本來就列出全部可提交任務）。
    budget = ui_text.DISCORD_MESSAGE_LIMIT - len("\n".join(lines)) - 150
    shown, remaining = ui_text.fit_items(task_lines, budget)
    lines.extend(shown)
    if remaining:
        lines.append(f"...另 {remaining} 筆請用下方選單查看")
    return ui_text.trim_message("\n".join(lines))


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
        return False, "❌ 沒有收到檔案，請回到 `/我的任務` 重新進入提交流程。"
    for attachment in attachments:
        size_error = direct_upload_size_error(attachment.size)
        if size_error:
            return False, size_error
    now = datetime.now(config.get_tenant().tz)
    if not base_folder_id:
        target, _ = await _resolve_submission_target(task, artist, now)
        if target is None:
            return False, (
                "❌ 無法建立公司的指定 Drive 資料夾，請聯絡營運確認 Drive 設定；"
                "請聯絡營運確認公司 Drive 設定。"
            )
        base_folder_id, task_path = target
        try:
            folder, folder_path = await _ensure_next_version_folder(
                base_folder_id=base_folder_id,
                parent_path=task_path,
                item_name=task.name,
                created_at=now,
            )
            folder_url = str(folder.get("webViewLink") or "")
        except Exception:
            logger.exception("建立版本化提交資料夾失敗")
            return False, "❌ 無法建立提交資料夾，請稍後再試。"
    folder_path = folder_path or [submission_folder_name(task.name, now)]
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
            "請稍後再試，或回到 `/我的任務` 重新提交。"
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
        await repositories.set_task_under_review(
            task.page_id, submission_page.get("url", "")
        )
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
    await notify_submission_reviewers(
        client, task,
        f"📥 **新提交待審核（檔案）**\n"
        f"任務：{task.name}｜{artist.name}｜{len(uploaded_files)} 個檔案（{file_size_mb}MB）\n"
        f"Drive：{folder_url}\nNotion：{submission_page.get('url', '')}",
    )
    resubmit_note = f"\n（之前交的 {replaced} 筆換成這次的，這筆是最新版）" if replaced else ""
    return True, (
        f"✅ **檔案收到了！「{task.name}」小律已經交給公司審核**\n"
        f"📁 {artist.name}專區／{folder_text}\n"
        f"檔案數：{len(uploaded_files)}｜{display_files}\n"
        f"審核結果出來，小律會馬上通知你。辛苦了！{resubmit_note}"
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
        self.tasks = tasks
        self.now = now
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
        if self.attachment is not None:
            await interaction.response.send_message(
                "此系統不接收 Discord 附件，請改上傳至指定 Google Drive 資料夾。",
                ephemeral=True,
            )
            return
        # 提交方式已全部停用（M4-03）：所有交件一律上傳到公司 Drive 的任務資料夾。
        await self.cog.start_file_upload_wait(interaction, task, drive_only=True)


class SubmitTaskView(discord.ui.View):
    def __init__(
        self,
        cog: "SubmissionsCog",
        tasks: list[repositories.TaskItem],
        now: datetime,
        attachment: discord.Attachment | None = None,
    ):
        super().__init__(timeout=600)
        self.cog = cog
        if tasks:
            self.add_item(SubmitTaskSelect(cog, tasks, now, attachment))

    @discord.ui.button(label="返回我的任務", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def back_to_my_tasks(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.return_to_my_tasks(interaction)


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
        super().__init__(timeout=_file_upload_wait_minutes() * 60)
        self.cog = cog
        self.task = task
        self.folder_url = folder_url
        self.target = target
        self.owner_id = owner_id

    @discord.ui.button(label="我已上傳到 Drive", style=discord.ButtonStyle.success)
    async def verify_drive_upload(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.owner_id is not None and interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ 這個交件入口是給打開它的那位同學用的喔。", ephemeral=True)
            return
        await self.cog.complete_drive_folder_submission(interaction)

    @discord.ui.button(label="返回任務選擇", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def back_to_tasks(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self.owner_id is not None and interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ 這個交件入口是給打開它的那位同學用的喔。", ephemeral=True)
            return
        await self.cog.return_to_submit_task_list(interaction)

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
        super().__init__(timeout=_file_upload_wait_minutes() * 60)
        self.cog = cog
        self.submission = submission
        self.result = result
        self.feedback = feedback
        self.reviewer = reviewer
        self.targets = targets
        self.owner_id = owner_id

    @discord.ui.button(label="貼 Drive 連結補回饋", style=discord.ButtonStyle.primary)
    async def submit_drive_link(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.owner_id is not None and interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ 這個回饋入口只給原本審核的人使用。", ephemeral=True)
            return
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

    @discord.ui.button(label="返回回饋選擇", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def back_to_feedback_choice(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self.owner_id is not None and interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ 這個回饋入口只給原本審核的人使用。", ephemeral=True)
            return
        self.cog.pending_review_feedback_files.pop(interaction.user.id, None)
        await interaction.response.edit_message(
            content="請選擇是否需要補充批改檔：",
            view=PostReviewAttachmentView(
                self.cog,
                self.submission,
                result=self.result,
                feedback=self.feedback,
                reviewer=self.reviewer,
                reviewer_id=interaction.user.id,
            ),
        )


class ReviewSubmissionSelect(discord.ui.Select):
    def __init__(self, cog: "SubmissionsCog", submissions: list[repositories.SubmissionItem]):
        self.cog = cog
        self.submissions = submissions
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
            view=ReviewActionView(
                self.cog, submission, interaction.user.id, self.submissions
            ),
            ephemeral=True,
        )


class ReviewSubmissionView(discord.ui.View):
    def __init__(self, cog: "SubmissionsCog", submissions: list[repositories.SubmissionItem]):
        super().__init__(timeout=600)
        self.add_item(ReviewSubmissionSelect(cog, submissions))

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="已取消審核，提交資料未變更。", view=None)
        self.stop()


class ReviewActionView(discord.ui.View):
    def __init__(
        self,
        cog: "SubmissionsCog",
        submission: repositories.SubmissionItem,
        reviewer_id: int,
        submissions: list[repositories.SubmissionItem],
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.submission = submission
        self.reviewer_id = reviewer_id
        self.submissions = submissions

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

    @discord.ui.button(label="返回審核清單", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def back_to_review_list(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._ensure_reviewer(interaction):
            return
        await interaction.response.edit_message(
            content=f"📮 待審核提交（{len(self.submissions)} 筆，最多顯示 25 筆）：",
            view=ReviewSubmissionView(self.cog, self.submissions),
        )


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

    @discord.ui.button(label="不需補批改檔", style=discord.ButtonStyle.success)
    async def no_feedback_file(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.reviewer_id:
            await interaction.response.send_message("❌ 這個按鈕只給原本審核的人使用。", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(
            content="✅ 已確認不需補批改檔。審核流程完成。",
            view=None,
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
        self.revision_days_input: discord.ui.TextInput | None = None
        if result in ("需修改", "退回"):
            self.revision_days_input = discord.ui.TextInput(
                label="修改期限（天數，1～7）",
                required=True,
                max_length=1,
                placeholder="例如：2",
            )
            self.add_item(self.revision_days_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        revision_days = None
        if self.revision_days_input is not None:
            revision_days = parse_revision_days(self.revision_days_input.value)
            if revision_days is None:
                await interaction.followup.send(
                    "❌ 修改期限只能填 1～7 的整數天數，請重新操作審核。",
                    ephemeral=True,
                )
                return
        await self.cog.complete_discord_review(
            interaction,
            self.submission,
            self.result,
            (self.feedback_input.value or "").strip(),
            revision_days=revision_days,
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

    async def edit_submit_task_list(self, interaction: discord.Interaction) -> None:
        """在 /我的任務 的原訊息中開啟可提交任務清單。"""
        try:
            artist = await repositories.find_artist_by_discord_id(interaction.user.id)
        except Exception:
            logger.exception("開啟提交清單時讀取藝人失敗")
            await interaction.edit_original_response(
                content="❌ 讀取藝人資料失敗，請稍後再試。",
                view=None,
            )
            return
        if artist is None:
            await interaction.edit_original_response(
                content="❌ 找不到你的藝人資料，請聯絡管理者確認 Notion 上的 Discord User ID 設定。",
                view=None,
            )
            return
        try:
            tasks = await repositories.get_discord_tasks_for_artist(artist.page_id)
        except Exception:
            logger.exception("讀取可提交任務失敗")
            await interaction.edit_original_response(
                content="❌ 讀取任務失敗，請稍後再試。",
                view=None,
            )
            return
        submittable = [task for task in tasks if repositories.is_task_submittable(task)]
        if not submittable:
            await interaction.edit_original_response(
                content=(
                    "目前沒有開放提交的任務。\n"
                    "（任務需在 Notion 勾「是否開放提交」且尚未進入審核／結案狀態）"
                ),
                view=SubmitTaskView(self, [], datetime.now(config.get_tenant().tz)),
            )
            return
        now = datetime.now(config.get_tenant().tz)
        submittable.sort(
            key=lambda task: (
                repositories.task_action_deadline(task)[0] is None,
                repositories.task_action_deadline(task)[0] or now,
            )
        )
        await interaction.edit_original_response(
            content=submit_task_intro_text(artist.name, submittable, now),
            view=SubmitTaskView(self, submittable, now),
        )

    async def return_to_my_tasks(self, interaction: discord.Interaction) -> None:
        tasks_cog = self.bot.get_cog("TasksCog")
        if tasks_cog is None:
            await interaction.response.edit_message(
                content="❌ 任務總覽尚未初始化，請稍後再試。",
                view=None,
            )
            return
        await tasks_cog.return_to_my_tasks(interaction)

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
        expires_at = now + timedelta(minutes=_file_upload_wait_minutes())
        created_at = now
        folder_url = ""
        folder_base_id = ""
        folder_path: list[str] = []
        state = load_drive_upload_state()
        reuse = reusable_drive_upload(state, interaction.user.id, task.page_id, now)
        if reuse is not None:
            created_at = reuse["created_at"]
            folder_base_id = reuse["folder_base_id"]
            folder_path = reuse["folder_path"]
            folder_url = reuse["folder_url"]
            target = (folder_base_id, folder_path)
        else:
            target, _ = await _resolve_submission_target(task, artist, now)
        if reuse is None and target is not None and drive_api.is_configured():
            base_folder_id, path_segments = target
            folder_base_id = base_folder_id
            try:
                folder, folder_path = await _ensure_next_version_folder(
                    base_folder_id=folder_base_id,
                    parent_path=path_segments,
                    item_name=task.name,
                    created_at=now,
                )
                folder_url = folder.get("webViewLink", "")
            except Exception:
                logger.exception("產生大檔 Drive 目標資料夾失敗")
        pending = PendingFileSubmission(
            task=task,
            artist=artist,
            channel_id=interaction.channel_id,
            created_at=created_at,
            expires_at=expires_at,
            folder_base_id=folder_base_id,
            folder_path=folder_path,
            folder_url=folder_url,
        )
        self.pending_file_submissions[interaction.user.id] = pending
        if folder_url and folder_base_id:
            try:
                save_drive_upload_state(
                    remember_drive_upload(state, interaction.user.id, task.page_id, pending, now)
                )
            except OSError:
                logger.exception("記錄 Drive 上傳資料夾失敗")

        reuse_note = "↩️ 沿用你上次開的資料夾，先前上傳的檔案都還在。\n" if reuse is not None else ""
        upload_intro = f"📁 **{task.name}**\n{reuse_note}請將檔案上傳至下方指定資料夾。\n\n"
        upload_controls = (
            "上傳完成後：\n"
            "・我已上傳到 Drive：檔案放入公司指定資料夾後按。\n"
            "・這則訊息只有你看得到，離開頻道後會消失。沒關係：回到 `/我的任務` → 提交任務 → "
            "選同一筆任務，會回到同一個資料夾。\n\n"
        )
        await interaction.followup.send(
            upload_intro
            + upload_controls
            + f"入口有效 {file_upload_wait_label()}。\n\n"
            + large_file_drive_instruction(
                task_name=task.name,
                folder_url=folder_url,
                drive_setup_error=bool(config.get_tenant().drive_root_folder_id)
                and drive_api.is_configured(),
            ),
            view=LargeFileSubmissionChoiceView(
                self, task, folder_url=folder_url, target=target,
                owner_id=interaction.user.id, drive_only=drive_only,
            ),
            ephemeral=True,
        )

    async def return_to_submit_task_list(self, interaction: discord.Interaction) -> None:
        """取消尚未完成的 Drive 提交，回到可提交任務清單。"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        self.pending_file_submissions.pop(interaction.user.id, None)
        await self.edit_submit_task_list(interaction)

    async def complete_drive_folder_submission(self, interaction: discord.Interaction) -> None:
        """驗證指定資料夾的新檔案，成功後建立一筆檔案提交。"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        pending = self.pending_file_submissions.get(interaction.user.id)
        if pending is None or not pending.folder_url or not pending.folder_base_id:
            await interaction.followup.send(
                "❌ 目前沒有可驗證的公司資料夾，請回到 `/我的任務` 重新進入提交流程；若仍失敗請聯絡公司。",
                ephemeral=True,
            )
            return
        folder_id = drive_archive.extract_drive_folder_id(pending.folder_url)
        if not folder_id:
            await interaction.followup.send(
                "❌ 找不到公司指定 Drive 資料夾，請聯絡公司檢查設定。",
                ephemeral=True,
            )
            return
        try:
            # Drive 的檔案索引不是立即一致；短暫重試避免藝人已上傳但剛好
            # 在 Drive API 尚未列出檔案時按下驗證。
            files: list[dict] = []
            for attempt in range(3):
                files = await drive_api.list_recent_files(folder_id, pending.created_at)
                if files or attempt == 2:
                    break
                await asyncio.sleep(2)
        except Exception:
            logger.exception("驗證 Drive 資料夾檔案失敗")
            await interaction.followup.send("❌ 小律剛剛讀不到 Drive 資料夾，請稍後再按一次。", ephemeral=True)
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
            await repositories.set_task_under_review(
                pending.task.page_id, submission_page.get("url", "")
            )
        except Exception:
            logger.exception("Drive 資料夾提交寫入 Notion 失敗")
            await interaction.followup.send("❌ 小律有看到你的檔案，但記到 Notion 時失敗了，請聯絡公司幫忙確認。", ephemeral=True)
            return
        self.pending_file_submissions.pop(interaction.user.id, None)
        try:
            save_drive_upload_state(
                forget_drive_upload(load_drive_upload_state(), interaction.user.id, pending.task.page_id)
            )
        except OSError:
            logger.exception("清除 Drive 上傳資料夾紀錄失敗")
        await audit.log_event(
            event_name=f"提交任務（Drive 資料夾）｜{pending.artist.name}｜{pending.task.name}",
            action_type="上傳",
            operator=pending.artist.name,
            discord_user_id=str(interaction.user.id),
            target_db="Submissions & File Index",
            target_url=submission_page.get("url", ""),
            after=f"{len(files)} 個檔案｜{total_mb}MB｜{pending.folder_url}",
        )
        await notify_submission_reviewers(
            interaction.client, pending.task,
            f"📥 **新提交待審核（Drive 資料夾）**\n"
            f"任務：{pending.task.name}｜{pending.artist.name}｜{len(files)} 個檔案（{total_mb}MB）\n"
            f"Drive：{pending.folder_url}\nNotion：{submission_page.get('url', '')}",
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
        expires_at = now + timedelta(minutes=_file_upload_wait_minutes())
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
            f"每個檔案請在 **{MAX_UPLOAD_MB}MB 以下**；入口 **{file_upload_wait_label()}**後失效。\n"
            "這則訊息只有你看得到，切去 Drive 上傳回來可能已消失；回到這個頻道隨便打一句話（例如「好了」），"
            "我會再給你按鈕。\n\n"
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
        task = (
            await repositories.get_task(submission.task_page_ids[0])
            if submission.task_page_ids
            else None
        )
        if task is None and submission.url:
            task = await repositories.find_task_by_latest_submission_url(submission.url)
        if task is None:
            return [], ["找不到關聯任務"]
        artist_map = await repositories.get_artist_map()
        targets: list[ReviewFeedbackDriveTarget] = []
        skipped: list[str] = []
        project_name = None
        if not task.drive_folder_url:
            try:
                project_name = await repositories.task_project_name(task)
            except Exception:
                logger.exception("讀取任務專案名稱失敗，暫不建立老師回饋資料夾：%s", task.page_id)
                return [], ["讀不到任務的關聯專案"]
        for artist_page_id in submission.artist_page_ids:
            artist = artist_map.get(artist_page_id)
            if not artist:
                continue
            target = drive_archive.resolve_review_feedback_target(
                artist.drive_folder_url, now, task, project_name
            )
            if target is None:
                skipped.append(artist.name)
                continue
            base_folder_id, parent_path = target
            try:
                folder, path_segments = await _ensure_next_version_folder(
                    base_folder_id=base_folder_id,
                    parent_path=parent_path,
                    item_name="老師回饋",
                    created_at=now,
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
            await message.channel.send("❌ 這次的上傳入口過期了，請回到 `/我的任務` 重新按「提交任務」，小律再幫你準備一次。")
            return
        if message.attachments:
            folder_hint = (
                f"\n指定資料夾：{pending.folder_url}" if pending.folder_url else ""
            )
            await message.channel.send(
                "此系統不接收 Discord 附件，請改上傳至指定 Google Drive 資料夾，"
                "再按「我已上傳到 Drive」完成驗證。"
                + folder_hint
            )
            return

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
            # 原本那則 ephemeral 訊息在老師切去 Drive 上傳後常已消失，按鈕跟著不見；
            # 已審過的提交也不會再出現在 /審核提交，所以在這裡把按鈕重新給一次。
            await message.channel.send(
                f"📎 **{pending.submission.name}** 的批改檔入口還開著（目前已收到 {len(pending.attachments)} 個附件）。\n"
                "・檔案已放到 Drive → 按「貼 Drive 連結補回饋」\n"
                "・要直接傳附件 → 在這個頻道上傳，傳完按「完成回饋」\n"
                "（如果你剛剛有上傳附件但我說 0 個，代表 Discord Bot 的 Message Content Intent 尚未開啟，請通知公司處理。）",
                view=self._review_feedback_controls(pending, message.author.id),
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
            "可以繼續上傳；全部完成後按下方「完成回饋」。",
            view=self._review_feedback_controls(pending, message.author.id),
        )

    def _review_feedback_controls(
        self, pending: PendingReviewFeedbackFile, owner_id: int,
    ) -> "ReviewFeedbackLargeFileChoiceView":
        return ReviewFeedbackLargeFileChoiceView(
            self,
            pending.submission,
            result=pending.result,
            feedback=pending.feedback,
            reviewer=pending.reviewer,
            targets=pending.targets,
            owner_id=owner_id,
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
        now = datetime.now(config.get_tenant().tz)
        try:
            history = await repositories.get_all_submissions()
        except Exception:
            logger.exception("讀取提交紀錄失敗，小律的那一句改用範本")
            history = []
        for artist_page_id in submission.artist_page_ids:
            artist = artist_map.get(artist_page_id)
            if not artist or not artist.discord_user_id:
                continue
            try:
                # 小律依這位藝人最近的表現說一句（M4-18）：只送數字，失敗回範本句
                facts = review_facts(result, submission, artist_page_id, history, now)
                line = await ritsu_voice.review_line(facts, state=notification_log.shared(), now=now)
                notification_log.persist()
                user = await self.bot.fetch_user(int(artist.discord_user_id))
                await user.send(
                    review_dm_text(
                        submission,
                        task_name=task_name,
                        result=result,
                        feedback=feedback,
                        reviewer=reviewer,
                        name=ritsu_voice.call_name(artist.name),
                        ritsu_line=line,
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

    async def _update_review_task_status(
        self,
        submission: repositories.SubmissionItem,
        result: str,
        *,
        revision_days: int | None = None,
    ) -> None:
        task_status = review_task_status_for_result(result)
        if not task_status:
            return
        for task_page_id in submission.task_page_ids:
            await repositories.update_task_status(task_page_id, task_status)
            if result in ("需修改", "退回"):
                # Discord 審核由審核人輸入 1～7 天；Notion 手動審核則保留既有欄位值。
                if revision_days is not None:
                    deadline = datetime.now(config.get_tenant().tz) + timedelta(days=revision_days)
                    await repositories.set_task_revision_deadline(task_page_id, deadline)
            else:
                await repositories.set_task_revision_deadline(task_page_id, None)

    async def complete_discord_review(
        self,
        interaction: discord.Interaction,
        submission: repositories.SubmissionItem,
        result: str,
        feedback: str,
        *,
        revision_days: int | None = None,
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
            await self._update_review_task_status(
                submission,
                result,
                revision_days=revision_days,
            )
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
            "若需要，可按「附批改檔」；不需要時按「不需補批改檔」完成流程。",
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
            reviewer = submission.reviewer or "老師／公司"
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
                    # 「提醒」是 Notion 操作類型既有的選項；寫沒有的值 Notion 會自動新增，
                    # 會讓下拉多出未定義選項，也讓同類事件散在兩個名字下。
                    action_type="提醒", operator="System", source="Notion",
                    target_db="Submissions & File Index", target_url=submission.url,
                    after=f"{submission.status}｜通知 {sent} 位藝人",
                )
            except Exception:
                logger.exception("審核結果通知失敗（submission=%s）", submission.page_id)

    @review_poll_loop.before_loop
    async def before_review_poll(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="審核提交", description="管理端：審核待審核提交並通知藝人")
    @app_commands.default_permissions(administrator=True)
    @is_admin()
    async def review_submission(self, interaction: discord.Interaction):
        if not await channel_guard.ensure_admin_channel(interaction):
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
