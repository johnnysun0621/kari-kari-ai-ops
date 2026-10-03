"""月度時間池到正式 Task 發布的持久化 SLA 狀態。"""
import json
import logging
from dataclasses import asdict, dataclass, fields
from pathlib import Path

logger = logging.getLogger(__name__)

_STATE_PATH = Path(".state/scheduling_sla.json")
_loaded = False


@dataclass
class CollectionWorkflow:
    workflow_id: str
    month: str
    created_ts: int
    fill_deadline_ts: int
    matched_ts: int = 0


@dataclass
class TaskWorkflow:
    task_page_id: str
    month: str
    started_ts: int
    completed_ts: int = 0


@dataclass(frozen=True)
class SLAEvent:
    key: str
    message: str


_collections: dict[str, CollectionWorkflow] = {}
_tasks: dict[str, TaskWorkflow] = {}
_sent_events: set[str] = set()


def _known_fields(cls, item: dict) -> dict:
    """只取目前 dataclass 還存在的欄位；舊狀態檔留下的欄位（例如已移除的老師確認
    時間）直接忽略，避免改 schema 後讀舊檔就整個崩掉。"""
    allowed = {f.name for f in fields(cls)}
    return {key: value for key, value in item.items() if key in allowed}


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return
    for item in raw.get("collections", []):
        workflow = CollectionWorkflow(**_known_fields(CollectionWorkflow, item))
        _collections[workflow.workflow_id] = workflow
    for item in raw.get("tasks", []):
        workflow = TaskWorkflow(**_known_fields(TaskWorkflow, item))
        _tasks[workflow.task_page_id] = workflow
    _sent_events.update(str(value) for value in raw.get("sent_events", []))


def _save() -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(
            json.dumps(
                {
                    "collections": [asdict(item) for item in _collections.values()],
                    "tasks": [asdict(item) for item in _tasks.values()],
                    "sent_events": sorted(_sent_events),
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
    except OSError:
        logger.exception("排課 SLA 狀態寫入失敗")


def register_collection(month: str, created_ts: int, fill_deadline_ts: int) -> str:
    _load()
    workflow_id = f"{month}:{fill_deadline_ts}"
    _collections[workflow_id] = CollectionWorkflow(
        workflow_id=workflow_id,
        month=month,
        created_ts=created_ts,
        fill_deadline_ts=fill_deadline_ts,
    )
    _save()
    return workflow_id


def deregister_collection(month: str, fill_deadline_ts: int) -> bool:
    """收集被取消時一併移除 SLA 追蹤，否則 48／72 小時後還會發升級提醒。

    回傳是否真的移除了（找不到就回 False，讓呼叫端知道狀態不一致）。
    """
    _load()
    removed = _collections.pop(f"{month}:{int(fill_deadline_ts)}", None)
    if removed is None:
        return False
    _save()
    return True


def mark_pool_matched(month: str, matched_ts: int) -> None:
    _load()
    candidates = [item for item in _collections.values() if item.month == month]
    if not candidates:
        return
    latest = max(candidates, key=lambda item: item.created_ts)
    latest.matched_ts = matched_ts
    _save()


def register_task(
    task_page_id: str,
    month: str,
    created_ts: int,
) -> None:
    _load()
    collections = [item for item in _collections.values() if item.month == month]
    started_ts = min(
        [item.created_ts for item in collections] or [created_ts]
    )
    _tasks[task_page_id] = TaskWorkflow(
        task_page_id=task_page_id,
        month=month,
        started_ts=started_ts,
    )
    _save()


def mark_completed(task_page_id: str, completed_ts: int) -> None:
    _load()
    workflow = _tasks.get(task_page_id)
    if workflow is None:
        return
    workflow.completed_ts = completed_ts
    _save()


def due_events(now_ts: int) -> list[SLAEvent]:
    """這些訊息帶 Notion 連結，只能發到控方頻道。

    呼叫端（cogs/reminders._check_scheduling_sla）在 ADMIN_NOTIFY_CHANNEL_ID 沒設定時
    會 fallback 到 SCHEDULE_CHANNEL_ID，而那是藝人看得到的頻道——平轉到新租戶時
    務必先設好 ADMIN_NOTIFY_CHANNEL_ID，否則等於把 Notion 入口發給藝人。
    """
    _load()
    events: list[SLAEvent] = []
    for workflow in _collections.values():
        key = f"collection:{workflow.workflow_id}:match_due"
        if (
            not workflow.matched_ts
            and now_ts >= workflow.fill_deadline_ts + 12 * 3600
            and key not in _sent_events
        ):
            events.append(SLAEvent(
                key,
                f"⏰ **{workflow.month} 時間池已截止超過半天**，請控方執行 `/從時間池建立任務`。",
            ))
    for workflow in _tasks.values():
        if workflow.completed_ts:
            continue
        age = now_ts - workflow.started_ts
        if age >= 48 * 3600:
            key = f"task:{workflow.task_page_id}:sla_48h"
            if key not in _sent_events:
                events.append(SLAEvent(
                    key,
                    f"🔴 排課流程已超過 48 小時：<https://www.notion.so/{workflow.task_page_id.replace('-', '')}>",
                ))
        if age >= 72 * 3600:
            key = f"task:{workflow.task_page_id}:stale_72h"
            if key not in _sent_events:
                events.append(SLAEvent(
                    key,
                    f"⚠️ 候選時段已超過 72 小時，可能失效，建議重新收集：<https://www.notion.so/{workflow.task_page_id.replace('-', '')}>",
                ))
    return events


def mark_event_sent(key: str) -> None:
    _load()
    _sent_events.add(key)
    _save()


def reset_for_tests(path: Path | None = None) -> None:
    global _STATE_PATH, _loaded
    if path is not None:
        _STATE_PATH = path
    _collections.clear()
    _tasks.clear()
    _sent_events.clear()
    _loaded = False
