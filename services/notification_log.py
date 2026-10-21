"""任務通知的本機持久化紀錄（M3-23）。

兩件事都要撐過 bot 重啟，否則每重啟一次就重發一輪：
1. 截止前提醒已送過哪些「任務＋階段＋期限日」——取代原本只存記憶體的去重。
   期限日放進 key：控方延期後，新的期限會重新觸發提醒，這是刻意的。
2. 每則任務通知（帶「收到任務」按鈕的私訊）的訊息編號，藝人按下收到後，
   才找得到同一任務之前發過的那幾則，把它們改成已收到。
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

STATE_PATH = Path(".state/task_notification_log.json")
RETENTION_DAYS = 45


def load(path: Path | None = None) -> dict:
    path = path or STATE_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
        return {"reminders": {}, "messages": {}}
    if not isinstance(raw, dict):
        return {"reminders": {}, "messages": {}}
    raw.setdefault("reminders", {})
    raw.setdefault("messages", {})
    return raw


def save(state: dict, path: Path | None = None) -> None:
    path = path or STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


_shared: dict | None = None


def shared() -> dict:
    """整個 bot process 共用同一份狀態：提醒 loop 與按鈕 callback 各自 load／save
    會互相蓋掉對方剛寫的紀錄，所以一律經由這裡取用，再用 persist() 寫回。"""
    global _shared
    if _shared is None:
        _shared = load()
    return _shared


def persist() -> None:
    if _shared is not None:
        save(_shared)


def reset_for_tests() -> None:
    global _shared
    _shared = None


def reminder_key(task_id: str, stage: str, deadline: datetime | None) -> str:
    return f"{task_id}|{stage}|{deadline.date().isoformat() if deadline else '-'}"


def reminder_sent(state: dict, task_id: str, stage: str, deadline: datetime | None) -> bool:
    return reminder_key(task_id, stage, deadline) in state["reminders"]


def mark_reminder_sent(
    state: dict, task_id: str, stage: str, deadline: datetime | None, now: datetime
) -> None:
    state["reminders"][reminder_key(task_id, stage, deadline)] = now.isoformat()


def record_message(
    state: dict, task_id: str, user_id: int | str, channel_id: int, message_id: int, now: datetime,
    bundle_id: str | None = None,
) -> None:
    """bundle_id：這則是合併訊息（M3-27），同一則會記在裡面每一筆任務底下。"""
    per_task = state["messages"].setdefault(str(task_id), {})
    entry = {"channel_id": int(channel_id), "message_id": int(message_id), "sent_at": now.isoformat()}
    if bundle_id:
        entry["bundle"] = bundle_id
    per_task.setdefault(str(user_id), []).append(entry)


def messages_for(state: dict, task_id: str, user_id: int | str) -> list[dict]:
    return list(state["messages"].get(str(task_id), {}).get(str(user_id), []))


def forget_messages(state: dict, task_id: str, user_id: int | str) -> None:
    per_task = state["messages"].get(str(task_id), {})
    per_task.pop(str(user_id), None)
    if not per_task:
        state["messages"].pop(str(task_id), None)


def prune(state: dict, now: datetime, retention_days: int = RETENTION_DAYS) -> None:
    """清掉超過保留天數的紀錄，避免狀態檔無限變大。"""
    cutoff = now - timedelta(days=retention_days)

    def fresh(stamp: str) -> bool:
        try:
            return datetime.fromisoformat(stamp) >= cutoff
        except (TypeError, ValueError):
            return False

    state["reminders"] = {k: v for k, v in state["reminders"].items() if fresh(v)}
    for task_id in list(state["messages"]):
        per_task = state["messages"][task_id]
        for user_id in list(per_task):
            kept = [m for m in per_task[user_id] if fresh(m.get("sent_at", ""))]
            if kept:
                per_task[user_id] = kept
            else:
                per_task.pop(user_id)
        if not per_task:
            state["messages"].pop(task_id)


def received_suffix(received_at: datetime) -> str:
    return f"\n\n✅ 已收到（{received_at:%m/%d %H:%M}）"
