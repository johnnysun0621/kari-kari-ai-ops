"""任務派發通知的純邏輯與本機去重狀態。"""
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from services.repositories import TaskItem

STATE_PATH = Path(".state/task_dispatch_notifications.json")
ASSIGNMENT_STATE_PATH = Path(".state/task_assignment_notifications.json")


def notification_message(task: TaskItem) -> str:
    is_formal_event = bool(
        task.start and task.end and task.start_has_time and task.end_has_time
    )
    if is_formal_event:
        deadline_line = f"時間：{task.start:%m/%d %H:%M}–{task.end:%H:%M}\n"
    elif task.end is None:
        deadline_line = "截止：未設定\n"
    elif task.end_has_time:
        deadline_line = f"截止：{task.end:%m/%d %H:%M}\n"
    else:
        deadline_line = f"截止：{task.end:%m/%d}\n"
    description = _artist_facing_description(task.description)
    if not description:
        description = "Notion 尚未填寫任務說明。"
    if len(description) > 900:
        description = description[:897].rstrip() + "..."
    extra_lines = []
    for label, value in (
        ("課程類別", task.course_category),
        ("負責老師", _description_value(task.description, "負責老師")),
    ):
        if value:
            extra_lines.append(f"{label}：{value}")
    extra_text = ("\n" + "\n".join(extra_lines)) if extra_lines else ""
    submit_text = (
        f"提交方式：{task.submit_method}"
        if task.submit_method
        else "提交方式／任務說明\n"
        f"{description}"
    )
    description_block = "" if not task.submit_method else f"\n\n**任務內容**\n{description}"
    return (
        f"📌 **新任務通知**\n"
        f"任務：**{task.name}**\n"
        f"類型：{task.task_type or '任務'}\n"
        f"{deadline_line}"
        f"{extra_text}\n"
        f"{submit_text}{description_block}\n\n"
        f"請先按「收到任務」確認你已看到這項任務；完成後再依指定方式提交。"
    )


def is_assignment_dispatchable(task: TaskItem) -> bool:
    """課後作業已由控方開放，且仍需通知關聯藝人。"""
    return bool(
        task.discord_visible
        and task.open_for_submission
        and task.artist_page_ids
        and task.status not in {"已完成", "已取消", "已封存"}
    )


def assignment_notification_message(task: TaskItem) -> str:
    """課後作業通知；只呈現作業要求，不呈現課程排程候選。"""
    description = _artist_facing_description(task.description) or "Notion 尚未填寫作業要求。"
    if len(description) > 900:
        description = description[:897].rstrip() + "..."
    deadline = "未設定"
    if task.end:
        deadline = task.end.strftime("%m/%d %H:%M" if task.end_has_time else "%m/%d")
    submit_method = task.submit_method or "請依任務說明"
    return (
        f"📚 **課後作業通知**\n"
        f"任務：**{task.name}**\n"
        f"截止：{deadline}\n"
        f"提交方式：{submit_method}\n\n"
        f"**作業要求**\n{description}\n\n"
        f"請先按「收到任務」確認你已看到這項作業；完成後再依指定方式提交。"
    )


_INTERNAL_DESCRIPTION_PREFIXES = (
    "安排來源：",
    "安排類型：",
    "任務類型：",
    "課程類別：",
    "負責老師：",
    "需要時長：",
    "候選時段",
    "此 Task 同時管理",
    "正式時間請由營運",
)


def _description_value(description: str, field_name: str) -> str:
    prefix = f"{field_name}："
    for line in description.splitlines():
        if line.strip().startswith(prefix):
            return line.strip()[len(prefix):].strip()
    return ""


def _artist_facing_description(description: str) -> str:
    """移除控方排程欄位，保留真正給藝人的任務說明。"""
    kept: list[str] = []
    for line in description.splitlines():
        stripped = line.strip()
        if (
            not stripped
            or any(stripped.startswith(prefix) for prefix in _INTERNAL_DESCRIPTION_PREFIXES)
            or re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}\s*[~〜–-]\s*\d{2}:\d{2}$", stripped)
        ):
            continue
        kept.append(stripped)
    return "\n".join(kept).strip()


def is_public_announcement_task(task: TaskItem, public_task_types: tuple[str, ...]) -> bool:
    """判斷 Task 是否需要額外發到公開公告頻道。"""
    return (task.task_type or "").strip() in {value.strip() for value in public_task_types}


def public_announcement_message(task: TaskItem) -> str:
    """產生公開公告；不放入藝人私訊用的提交按鈕與私人內容。"""
    time_text = "時間待定"
    if task.start and task.end:
        time_text = f"{task.start:%m/%d %H:%M}–{task.end:%H:%M}"
    return (
        f"📢 **{task.task_type or '正式安排'}公告**\n"
        f"任務：**{task.name}**\n"
        f"時間：{time_text}\n"
        f"{task.url or ''}"
    ).strip()


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


def is_resendable_task(task: TaskItem) -> bool:
    """管理端重送清單：只列仍未按「收到任務」的可見任務。"""
    return (
        task.discord_visible
        and task.discord_acknowledged_at is None
        and task.status not in {"已完成", "已取消", "已封存"}
    )


def should_run_acknowledgement_scan(
    last_scan_at: datetime | None,
    now: datetime,
    interval_hours: float = 12,
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


def find_hidden_tasks_needing_notification_reset(
    tasks: list[TaskItem],
) -> list[TaskItem]:
    """找出已隱藏、但仍殘留上次派發紀錄的任務。"""
    return [
        task
        for task in tasks
        if not task.discord_visible
        and (
            task.discord_notified_at is not None
            or task.discord_acknowledged_at is not None
        )
    ]


def load_state(path: Path = STATE_PATH) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_assignment_state(path: Path = ASSIGNMENT_STATE_PATH) -> dict:
    return load_state(path)


def save_assignment_state(state: dict, path: Path = ASSIGNMENT_STATE_PATH) -> None:
    save_state(state, path)


def was_assignment_notified(task_id: str, state: dict) -> bool:
    return str(task_id) in {str(value) for value in state.get("notified_task_ids", [])}


def mark_assignment_notified(task_id: str, state: dict) -> dict:
    notified = [str(value) for value in state.get("notified_task_ids", [])]
    if str(task_id) not in notified:
        notified.append(str(task_id))
    return {**state, "notified_task_ids": notified}


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
