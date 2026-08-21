"""跨 cog 共用的 Discord 訊息文字 helper（純函式，沿用 channel_guard 的共用模組模式）。"""
from datetime import datetime

from services import repositories

DISCORD_MESSAGE_LIMIT = 1900  # 留 buffer 的 2000 字上限


def trim_message(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 18].rstrip() + "\n...清單過長已截斷"


def deadline_text(task: repositories.TaskItem, now: datetime) -> str:
    """任務截止的人話描述：截止 7/15（剩 3 天）／（今天）／（已過 2 天）。"""
    is_revision = task.status == "需修改" and task.revision_deadline is not None
    if is_revision:
        deadline, has_time = task.revision_deadline, True
    else:
        deadline, has_time = repositories.task_action_deadline(task)
    if not deadline:
        return "無截止日"
    day_diff = (deadline.date() - now.date()).days
    date_text = f"{deadline.month}/{deadline.day}"
    if has_time:
        date_text += deadline.strftime(" %H:%M")
    prefix = "修改期限" if is_revision else "截止"
    if day_diff < 0:
        return f"{prefix} {date_text}（已過 {-day_diff} 天）"
    if day_diff == 0:
        return f"{prefix} {date_text}（今天）"
    return f"{prefix} {date_text}（剩 {day_diff} 天）"
