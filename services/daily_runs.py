"""每天一次的發送（藝人早報、營運早報、每日催辦）的送達狀態（M3-11，2026-10-21）。

原本三支都是「先記下今天已跑，再發送」：防止每 30 分鐘重複轟炸，但 Discord 或 Notion 一時失敗，
當天就整個漏掉，也分不出「試過了」和「真的送到了」。改成：

- 到了時間就跑；全部送達才記成今天完成，之後同一天不再跑。
- 有人送失敗、或讀 Notion 失敗：下一輪（REMINDER_INTERVAL_MINUTES，預設 30 分鐘）重試，同一天最多 MAX_ATTEMPTS 次。
- 已送達的收件人記在 delivered，重試時跳過，不會重複收到。
- 狀態存在 notification_log 的共用 state（.state/），重開 bot 後同一天不重發、也記得重試到第幾次。

這裡只管狀態；發送本身在各自的 cog。呼叫端每次改完都要 notification_log.persist()。
"""
from datetime import datetime

MAX_ATTEMPTS = 3
STATE_KEY = "daily_runs"


def _entry(state: dict, job: str, now: datetime, legacy_last_date: str | None = None) -> dict:
    today = now.date().isoformat()
    runs = state.setdefault(STATE_KEY, {})
    entry = runs.get(job)
    if not isinstance(entry, dict) or entry.get("day") != today:
        # 舊版只記 *_last_date：上線當天已經跑過的，當作今天已完成，不要再發一次
        done = legacy_last_date == today
        entry = {"day": today, "attempts": 0, "done": done, "delivered": []}
        runs[job] = entry
    return entry


def should_run(state: dict, job: str, now: datetime, legacy_last_date: str | None = None) -> bool:
    """今天還沒完成、也還沒用完重試次數。"""
    entry = _entry(state, job, now, legacy_last_date)
    return not entry["done"] and entry["attempts"] < MAX_ATTEMPTS


def begin(state: dict, job: str, now: datetime) -> int:
    """開始第 N 次嘗試（先記次數再發送：中途當掉也算一次，不會無限重試）。回傳這是第幾次。"""
    entry = _entry(state, job, now)
    entry["attempts"] += 1
    return entry["attempts"]


def is_last_attempt(state: dict, job: str, now: datetime) -> bool:
    return _entry(state, job, now)["attempts"] >= MAX_ATTEMPTS


def delivered(state: dict, job: str, now: datetime) -> set[str]:
    return set(_entry(state, job, now)["delivered"])


def mark_delivered(state: dict, job: str, now: datetime, key: str) -> None:
    entry = _entry(state, job, now)
    if key not in entry["delivered"]:
        entry["delivered"].append(key)


def finish(state: dict, job: str, now: datetime, *, complete: bool, note: str = "") -> bool:
    """記下這次的結果。全部送達、或已經是最後一次嘗試，就記成今天完成。回傳是否完成。"""
    entry = _entry(state, job, now)
    entry["done"] = complete or entry["attempts"] >= MAX_ATTEMPTS
    entry["complete"] = complete
    if note:
        entry["note"] = note
    return entry["done"]
