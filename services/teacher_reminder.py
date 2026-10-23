"""課後提醒老師到 Discord 論壇發布（M5-01／M5-02）。

課程任務不判逾期（M5-03），所以這是課程類唯一的提醒來源。
- 課程結束（截止日）過 after_hours 小時後發第一次；之後每天在每日催辦的整點再催一次。
- 結案由控方手動，bot 不偵測論壇訊息：控方把任務改成終態就停（M5-02）。
- 最多催 max_days 天；還沒結案就改通知控方一次，不再催老師，避免騷擾。
"""
from datetime import datetime, time, timedelta

from services import ops_notice, repositories
from services.repositories import TaskItem


def class_ended_at(task: TaskItem) -> datetime | None:
    """課程結束時間：截止日；只填日期時視為當天 23:59（M3-04 的整天有效慣例）。"""
    if task.end is None:
        return None
    if task.end_has_time:
        return task.end
    return datetime.combine(task.end.date(), time(23, 59), tzinfo=task.end.tzinfo)


def is_course_task(task: TaskItem) -> bool:
    return repositories.task_has_project_type(task, repositories.PROJECT_TYPE_COURSE_TRAINING)


def next_action(
    task: TaskItem,
    now: datetime,
    record: dict | None,
    *,
    after_hours: float,
    daily_hour: int,
    max_days: int,
) -> str | None:
    """回傳這一輪要做什麼：'remind'（催老師）、'escalate'（改通知控方）或 None。"""
    if not is_course_task(task) or task.status in repositories.INACTIVE_TASK_STATUSES:
        return None
    ended = class_ended_at(task)
    if ended is None or now < ended + timedelta(hours=after_hours):
        return None
    record = record or {}
    if record.get("escalated"):
        return None
    count = int(record.get("count", 0))
    if count == 0:
        if now - ended > timedelta(days=max_days):
            return None  # 很久以前的課（例如上線前就上完的），不再開始催，免得一上線就洗版
        return "remind"  # 第一次：課後門檻一到就發，不等每日整點
    if record.get("last_date") == now.date().isoformat() or now.hour < daily_hour:
        return None
    return "escalate" if count >= max_days else "remind"


def teacher_message(task: TaskItem, artist_names: list[str]) -> str:
    ended = class_ended_at(task)
    when = ended.strftime("%m/%d %H:%M") if ended else "未設定"
    who = "、".join(artist_names) or "（未關聯藝人）"
    return (
        "📝 **課後提醒**\n"
        f"課程：**{task.name}**｜學生：{who}｜上課時間：{when}\n"
        "請到 Discord 論壇的藝人個人專區，發布這堂課的作業要求與評語。\n"
        "發布後由營運結案；結案前每天會提醒一次。"
    )


def escalation_message(task: TaskItem, teacher_names: list[str], days: int) -> str:
    who = "、".join(teacher_names) or "（未指定負責老師）"
    return (
        f"⚠️ **課後發布未結案**｜{task.name}\n"
        f"已提醒負責老師 {who} 共 {days} 天，任務仍未結案。"
        "請確認老師是否已在論壇發布；已發布請把任務結案，之後不會再提醒老師。"
    )


def escalation_note(task: TaskItem, teacher_names: list[str], days: int) -> str:
    """營運早報裡的一行（M3-25）。"""
    who = "、".join(teacher_names) or "（未指定負責老師）"
    return f"{who}｜{ops_notice.link(task.name, task.url)}｜已提醒 {days} 天仍未結案，請確認是否已在論壇發布"


def unreachable_note(task: TaskItem, teacher_names: list[str]) -> str:
    who = "、".join(teacher_names) or "（未指定）"
    return f"{who}｜{ops_notice.link(task.name, task.url)}｜老師聯絡不到（沒有 Discord ID 或私訊失敗），請轉告"


def unreachable_message(task: TaskItem, teacher_names: list[str]) -> str:
    who = "、".join(teacher_names) or "（未指定）"
    return (
        f"📝 **課後提醒（老師聯絡不到）**｜{task.name}\n"
        f"負責老師：{who}，沒有 Discord ID 或私訊失敗。請營運轉告老師到論壇發布作業要求與評語。"
    )


def teacher_digest_message(entries: list[tuple[TaskItem, list[str], int]]) -> str:
    """同一位老師這一輪有好幾堂課要發作業時，合成一則（M3-25 第三階段）。"""
    lines = ["📝 **課後提醒**｜請到 Discord 論壇各藝人的個人專區，發布這幾堂課的作業要求與評語："]
    for task, artist_names, count in entries:
        ended = class_ended_at(task)
        when = ended.strftime("%m/%d %H:%M") if ended else "未設定"
        who = "、".join(artist_names) or "（未關聯藝人）"
        nth = f"｜已提醒第 {count} 天" if count > 1 else ""
        lines.append(f"・**{task.name}**｜{who}｜{when}{nth}")
    lines.append("發布後由營運結案；結案前每天會提醒一次。")
    return "\n".join(lines)
