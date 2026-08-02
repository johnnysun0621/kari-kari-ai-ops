"""任務派發通知的純邏輯與本機去重狀態。"""
import json
from datetime import datetime, timedelta
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
    description = task.description.strip() or "Notion 尚未填寫任務說明。"
    if len(description) > 900:
        description = description[:897].rstrip() + "..."
    return (
        f"📌 **新任務通知**\n"
        f"任務：**{task.name}**\n"
        f"類型：{task.task_type or '任務'}\n"
        f"截止：{deadline}\n"
        f"提交方式：{submit_method}\n\n"
        f"**任務內容**\n{description}\n\n"
        f"請先按「收到任務」確認你已看到這項任務；完成後再依指定方式提交。"
    )


def acknowledgement_reminder_message(tasks: list[TaskItem]) -> str:
    """提醒藝人逐筆確認尚未收到的任務。"""
    lines = ["⏰ **以下任務尚未確認收到**", "Bot 將重新送出任務通知，請直接按對應的「收到任務」按鈕："]
    for task in tasks:
        deadline = "未設定"
        if task.end:
            deadline = task.end.strftime("%m/%d %H:%M" if task.end_has_time else "%m/%d")
        lines.append(f"・**{task.name}**｜截止：{deadline}")
    lines.append("\n每一筆任務都要分別確認；已按過的任務不會重新提醒。")
    return "\n".join(lines)


def should_run_acknowledgement_scan(
    last_scan_at: datetime | None,
    now: datetime,
    interval_hours: float = 6,
) -> bool:
    """判斷是否到了固定週期的未確認任務掃描時間。"""
    return last_scan_at is None or now - last_scan_at >= timedelta(hours=interval_hours)


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


def find_dispatch_tasks(
    tasks: list[TaskItem],
    state: dict,
) -> tuple[list[TaskItem], dict]:
    """找出尚未通知的可派發任務，包含 Bot 啟動前已存在的任務。"""
    current_ids = [task.page_id for task in tasks]
    pending = [
        task
        for task in tasks
        if task.discord_visible
        and task.status not in {"已完成", "已取消", "已封存"}
        and task.discord_notified_at is None
    ]
    # 已通知與否以 Notion 的 Discord 通知時間為準。
    return pending, {"initialized": True, "known_task_ids": current_ids}


def load_state(path: Path = STATE_PATH) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def forget_task(task_id: str, state: dict) -> dict:
    """移除未成功派發的任務，讓下一輪背景檢查可以重試。"""
    known_ids = [
        value for value in state.get("known_task_ids", [])
        if str(value) != str(task_id)
    ]
    return {**state, "known_task_ids": known_ids}


def acknowledgement_props(acknowledged_at: datetime) -> dict:
    """回傳 Notion 寫回收到確認的欄位；狀態由 repository 組合。"""
    return {"Discord 收到時間": {"date": {"start": acknowledged_at.isoformat()}}}


def can_acknowledge(task: TaskItem) -> bool:
    return task.status in {"尚未開始", "待提交", "進行中"}
