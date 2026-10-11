"""每日催辦（M3-11）：卡住的任務每天固定時間催一次。

只催三種狀態：已逾期、需修改催藝人；待審核催審核人員（沒指定或聯絡不到才發控方頻道）。
未到期的任務走截止前提醒，不在這裡。催辦不帶確認鈕（M3-12）。
每位藝人／審核人員一天只收一則彙整，不是每筆任務一則。
"""
from dataclasses import dataclass, field
from datetime import datetime

from services.repositories import Artist, SubmissionItem, TaskItem

ARTIST_NAG_STATUSES = ("已逾期", "需修改")
MAX_LINES = 20


def daily_nag_due(now: datetime, last_date: str | None, hour: int) -> bool:
    """今天是否該跑每日催辦：過了指定整點、今天還沒跑過。重啟後靠持久化日期不重複。"""
    return now.hour >= hour and last_date != now.date().isoformat()


@dataclass
class ArtistNag:
    artist: Artist
    overdue: list[TaskItem] = field(default_factory=list)
    revision: list[TaskItem] = field(default_factory=list)


def group_artist_nags(tasks: list[TaskItem], artist_map: dict[str, Artist]) -> list[ArtistNag]:
    """把已逾期／需修改、已派發給藝人的任務，依藝人分組。沒有 Discord ID 的藝人略過。"""
    nags: dict[str, ArtistNag] = {}
    for task in tasks:
        if task.status not in ARTIST_NAG_STATUSES or not task.discord_visible:
            continue
        for page_id in task.artist_page_ids:
            artist = artist_map.get(page_id)
            if not artist or not artist.discord_user_id:
                continue
            nag = nags.setdefault(page_id, ArtistNag(artist=artist))
            (nag.overdue if task.status == "已逾期" else nag.revision).append(task)
    return list(nags.values())


def _date(value: datetime | None, with_time: bool = False) -> str:
    if not value:
        return "未設定"
    return value.strftime("%m/%d %H:%M" if with_time else "%m/%d")


def _lines(items: list[str]) -> list[str]:
    shown = items[:MAX_LINES]
    if len(items) > MAX_LINES:
        shown.append(f"…另有 {len(items) - MAX_LINES} 筆")
    return shown


def artist_nag_message(nag: ArtistNag) -> str:
    """藝人的每日催辦。只放任務名稱與期限，不放 Notion 網址（M7-04）。"""
    lines = ["⏰ **每日待辦提醒**", "以下任務需要你處理："]
    if nag.overdue:
        lines.append("\n🔴 **已逾期**")
        lines += _lines([f"• {t.name[:60]}（截止 {_date(t.end)}）" for t in nag.overdue])
    if nag.revision:
        lines.append("\n🛠 **需修改**")
        lines += _lines([
            f"• {t.name[:60]}（修改期限 {_date(t.revision_deadline, with_time=True)}）"
            for t in nag.revision
        ])
    lines.append("\n處理好請用 `/我的任務` 重新提交；有問題請聯絡營運。")
    return "\n".join(lines)


def pending_review_tasks(
    submissions: list[SubmissionItem],
    tasks_by_id: dict[str, TaskItem],
    now: datetime,
    after_hours: float,
    is_due,
) -> dict[str, list[SubmissionItem]]:
    """待審核且已過首次催辦門檻的提交，依任務分組（只收還在進行中的任務）。"""
    grouped: dict[str, list[SubmissionItem]] = {}
    for submission in submissions:
        if not is_due(submission.created_at, now, after_hours):
            continue
        for task_id in submission.task_page_ids:
            if task_id in tasks_by_id:
                grouped.setdefault(task_id, []).append(submission)
    return grouped


def review_nag_message(entries: list[tuple[TaskItem, SubmissionItem]], after_hours: float, title: str) -> str:
    lines = [title, f"以下提交已超過 {after_hours:g} 小時仍未審核："]
    lines += _lines([f"• {task.name[:50]}｜{submission.name[:40]}" for task, submission in entries])
    lines.append("\n請用 `/審核提交` 完成審核並回填結果。")
    return "\n".join(lines)
