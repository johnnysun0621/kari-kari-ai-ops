"""月度排程固定公告區（M6-02）。

公告頻道每個月固定一則訊息，列出當月已派發的公開類型任務（PUBLIC_TASK_TYPES），
之後隨布告欄同步（BULLETIN_SYNC_INTERVAL_MINUTES）原地編輯，不另發新訊息洗版。
內容有變才編輯；藝人看得到，所以不放任何 Notion 網址（M7-04）。
"""
import calendar
import hashlib
import json
from datetime import date, datetime
from pathlib import Path

from services.repositories import TaskItem

STATE_PATH = Path(".state/monthly_board.json")
HIDDEN_STATUSES = {"已取消", "已封存"}


def load_state(path: Path | None = None) -> dict:
    try:
        raw = json.loads((path or STATE_PATH).read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def save_state(state: dict, path: Path | None = None) -> None:
    path = path or STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


WEEKDAYS = "一二三四五六日"
MESSAGE_LIMIT = 1900


def month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def month_range(day: date) -> tuple[date, date]:
    last = calendar.monthrange(day.year, day.month)[1]
    return date(day.year, day.month, 1), date(day.year, day.month, last)


def _anchor(task: TaskItem) -> datetime | None:
    return task.start or task.end


def board_tasks(tasks: list[TaskItem], day: date, public_types: tuple[str, ...]) -> list[TaskItem]:
    """當月、已派發、公開類型、沒被取消或封存的任務，依時間排序。"""
    first, last = month_range(day)
    wanted = {value.strip() for value in public_types}
    picked = []
    for task in tasks:
        anchor = _anchor(task)
        if anchor is None or not (first <= anchor.date() <= last):
            continue
        if not task.discord_visible or task.status in HIDDEN_STATUSES:
            continue
        if (task.task_type or "").strip() not in wanted:
            continue
        picked.append(task)
    return sorted(picked, key=lambda t: (_anchor(t), t.name))


def _when(task: TaskItem) -> str:
    anchor = _anchor(task)
    text = f"{anchor:%m/%d}（{WEEKDAYS[anchor.weekday()]}）"
    if task.start and task.start_has_time:
        text += f" {task.start:%H:%M}"
        if task.end and task.end_has_time and task.end.date() == task.start.date():
            text += f"–{task.end:%H:%M}"
    return text


def board_lines(tasks: list[TaskItem], artist_names: dict[str, str]) -> list[str]:
    lines = []
    for task in tasks:
        who = "、".join(artist_names[p] for p in task.artist_page_ids if p in artist_names)
        lines.append(f"• {_when(task)}｜{task.task_type}｜{task.name[:50]}" + (f"｜{who}" if who else ""))
    return lines


def render(day: date, lines: list[str], public_types: tuple[str, ...], updated_at: datetime) -> str:
    header = [
        f"🗓️ **{day.year} 年 {day.month} 月排程**（{'／'.join(public_types)}）",
        f"最後更新 {updated_at:%m/%d %H:%M}｜個人任務請用 `/我的任務` 查看",
        "",
    ]
    body = lines or ["本月目前沒有公開排程。"]
    text = "\n".join(header + body)
    if len(text) <= MESSAGE_LIMIT:
        return text
    kept: list[str] = []
    for line in body:
        if len("\n".join(header + kept + [line])) > MESSAGE_LIMIT - 40:
            break
        kept.append(line)
    return "\n".join(header + kept + [f"…另有 {len(body) - len(kept)} 筆，請用 `/我的任務` 查看"])


def fingerprint(lines: list[str]) -> str:
    """不含更新時間：內容沒變就不必編輯訊息。"""
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]
