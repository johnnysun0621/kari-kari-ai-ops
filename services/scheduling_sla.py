"""月度時間池到正式 Task 發布的持久化 SLA 狀態。"""
import json
import logging
from dataclasses import asdict, dataclass
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
    teacher_deadline_ts: int = 0
    teacher_responded_ts: int = 0
    completed_ts: int = 0


@dataclass(frozen=True)
class SLAEvent:
    key: str
    message: str


_collections: dict[str, CollectionWorkflow] = {}
_tasks: dict[str, TaskWorkflow] = {}
_sent_events: set[str] = set()


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
        workflow = CollectionWorkflow(**item)
        _collections[workflow.workflow_id] = workflow
    for item in raw.get("tasks", []):
        workflow = TaskWorkflow(**item)
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
    teacher_deadline_ts: int = 0,
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
        teacher_deadline_ts=teacher_deadline_ts,
    )
    _save()


def mark_teacher_responded(task_page_id: str, responded_ts: int) -> None:
    _load()
    workflow = _tasks.get(task_page_id)
    if workflow is None:
        return
    workflow.teacher_responded_ts = responded_ts
    _save()


def mark_completed(task_page_id: str, completed_ts: int) -> None:
    _load()
    workflow = _tasks.get(task_page_id)
    if workflow is None:
        return
    workflow.completed_ts = completed_ts
    _save()


def due_events(now_ts: int) -> list[SLAEvent]:
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
        if (
            workflow.teacher_deadline_ts
            and not workflow.teacher_responded_ts
            and now_ts >= workflow.teacher_deadline_ts
        ):
            key = f"task:{workflow.task_page_id}:teacher_overdue"
            if key not in _sent_events:
                events.append(SLAEvent(
                    key,
                    f"🔴 課程 Task 老師確認已超過 24 小時：<https://www.notion.so/{workflow.task_page_id.replace('-', '')}>",
                ))
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
