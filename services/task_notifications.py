"""任務派發通知的純邏輯與本機去重狀態。"""
import json
from datetime import datetime
from pathlib import Path

from services.repositories import TaskItem

STATE_PATH = Path(".state/task_dispatch_notifications.json")


def notification_message(task: TaskItem) -> str:
    if task.end is None:
        deadline = "未設定"
    elif task.end_has_time:
        deadline = task.end.strftime("%m/%d %H:%M")
    else:
        deadline = task.end.strftime("%m/%d")
    submit_method = task.submit_method or "請依任務說明"
    return (
        f"📌 **新任務通知**\n"
        f"任務：**{task.name}**\n"
        f"類型：{task.task_type or '任務'}\n"
        f"截止：{deadline}\n"
        f"提交方式：{submit_method}\n\n"
        f"請先按「收到任務」確認你已看到這項任務；完成後再依 Notion 指定方式提交。"
    )


def find_unseen_tasks(
    tasks: list[TaskItem],
    state: dict,
) -> tuple[list[TaskItem], dict]:
    """找出新出現的任務；第一次啟用只建立基準，不通知歷史任務。"""
    known_ids = {str(value) for value in state.get("known_task_ids", [])}
    current_ids = [task.page_id for task in tasks]
    if not state.get("initialized"):
        return [], {"initialized": True, "known_task_ids": current_ids}
    unseen = [task for task in tasks if task.page_id not in known_ids]
    return unseen, {"initialized": True, "known_task_ids": current_ids}


def load_state(path: Path = STATE_PATH) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def acknowledgement_props(acknowledged_at: datetime) -> dict:
    """回傳 Notion 寫回收到確認的欄位；狀態由 repository 組合。"""
    return {"Discord 收到時間": {"date": {"start": acknowledged_at.isoformat()}}}


def can_acknowledge(task: TaskItem) -> bool:
    return task.status in {"尚未開始", "待提交", "進行中"}
