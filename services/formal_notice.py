"""正式 Task 通告的本機冪等狀態。

Notion Audit Log 保留操作軌跡；本機狀態負責在 Discord 互動層快速阻擋同一
Task 重複發布。未來多 instance 部署時應改成共享儲存。
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_STATE_PATH = Path(".state/formal_notices.json")
_published_task_ids: set[str] = set()
_loaded = False


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return
    _published_task_ids.update(str(value) for value in raw.get("published_task_ids", []))


def _save() -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(
            json.dumps({"published_task_ids": sorted(_published_task_ids)}, indent=1),
            encoding="utf-8",
        )
    except OSError:
        logger.exception("正式通告狀態寫入失敗")


def was_published(task_page_id: str) -> bool:
    _load()
    return task_page_id in _published_task_ids


def mark_published(task_page_id: str) -> None:
    _load()
    _published_task_ids.add(task_page_id)
    _save()


def reset_for_tests(path: Path | None = None) -> None:
    global _STATE_PATH, _loaded
    if path is not None:
        _STATE_PATH = path
    _published_task_ids.clear()
    _loaded = False
