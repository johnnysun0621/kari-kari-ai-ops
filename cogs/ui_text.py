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
    if not task.end:
        return "無截止日"
    day_diff = (task.end.date() - now.date()).days
    date_text = f"{task.end.month}/{task.end.day}"
    if task.end_has_time:
        date_text += task.end.strftime(" %H:%M")
    if day_diff < 0:
        return f"截止 {date_text}（已過 {-day_diff} 天）"
    if day_diff == 0:
        return f"截止 {date_text}（今天）"
    return f"截止 {date_text}（剩 {day_diff} 天）"
