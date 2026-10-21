"""同一輪多筆任務合併成一則通知（M3-27，Johnny 2026-10-21）。

控方一次勾好幾筆 Discord 顯示、一次重送好幾筆、或一次開放好幾筆課後作業時，原本藝人會收到一張一張疊起來的卡，
重送時舊卡也不會消失。改成：

- 同一輪、同一位藝人 2 筆以上 → 一則合併訊息：清單＋「看某一筆的詳細」（展開在同一則）＋「✅ 全部收到」＋「⚠️ 某一筆有問題」
- 只有 1 筆 → 原本的任務卡（收到任務＋有問題）
- 相關同仁也合併成一則清單（沒有按鈕）
- 發新通知前，同一任務發給同一個人的舊通知先刪掉（舊的合併訊息還有別的任務沒被取代就保留）
- 收到之後仍可回報問題；已交件、修改中的任務不能用按鈕回報

這裡是純函式＋.state 讀寫；Discord 元件與發送在 cogs/tasks_artist.py。
"""
import uuid
from datetime import datetime, timedelta

from services.notion_links import strip_notion_links
from services.repositories import TaskItem

MAX_BUNDLE = 25  # Discord 下拉選單最多 25 個選項
MESSAGE_LIMIT = 1900
RETENTION_DAYS = 45
KIND_DISPATCH = "dispatch"
KIND_ASSIGNMENT = "assignment"
# 已交件（待審核）或修改中（需修改）的任務退回「尚未開始」會打亂審核流程，請藝人直接聯絡控方
CONCERN_STATUSES = {"尚未開始", "進行中", "待提交", None, ""}
CONCERN_BLOCKED_REPLY = "這筆已經交件或正在修改中，不能用按鈕回報。有問題請直接聯絡控方。"

_TITLES = {KIND_DISPATCH: "📌 **任務通知**", KIND_ASSIGNMENT: "📚 **課後作業通知**"}
_FOOTERS = {
    KIND_DISPATCH: "想看某一筆的完整內容，用下方選單；看過後按「✅ 全部收到」。某一筆不行就用「⚠️ 某一筆有問題」。",
    KIND_ASSIGNMENT: "這幾筆現在可以交了。想看作業要求，用下方選單；看過後按「✅ 全部收到」。交件請用 `/我的任務` → 提交任務。",
}


def can_report_concern(task: TaskItem) -> bool:
    return task.status in CONCERN_STATUSES


def chunks(tasks: list[TaskItem]) -> list[list[TaskItem]]:
    return [tasks[i:i + MAX_BUNDLE] for i in range(0, len(tasks), MAX_BUNDLE)]


def _limit(text: str) -> str:
    if len(text) <= MESSAGE_LIMIT:
        return text
    return text[: MESSAGE_LIMIT - 30].rstrip() + "\n…（完整清單請用 `/我的任務`）"


def bundle_text(kind: str, tasks: list[TaskItem], deadline_text) -> str:
    lines = [f"{_TITLES[kind]}（{len(tasks)}）"]
    lines += [f"・{strip_notion_links(task.name)}｜{deadline_text(task)}" for task in tasks]
    lines += ["", _FOOTERS[kind]]
    return _limit("\n".join(lines))


def staff_bundle_text(tasks: list[TaskItem], artist_names, deadline_text) -> str:
    """相關同仁的合併知會（沒有按鈕）。artist_names(task) 回傳這筆的藝人名稱清單。"""
    lines = [f"👥 **同仁知會（{len(tasks)}）**：你是這些任務的相關同仁，這則僅供通知，不需要按任何按鈕。"]
    for task in tasks:
        names = "、".join(artist_names(task))
        lines.append(f"・{strip_notion_links(task.name)}｜{deadline_text(task)}" + (f"｜藝人：{names}" if names else ""))
    return _limit("\n".join(lines))


def received_bundle_line(count: int, at: datetime) -> str:
    return f"✅ 已收到 {count} 筆任務｜{at:%m/%d %H:%M}"


# ---------- .state（notification_log 的共用 state） ----------

def new_bundle_id() -> str:
    return uuid.uuid4().hex[:12]


def remember_bundle(state: dict, bundle_id: str, user_id: int | str, kind: str, task_ids: list[str], now: datetime) -> None:
    bundles = state.setdefault("bundles", {})
    bundles[bundle_id] = {
        "user_id": str(user_id), "kind": kind, "task_ids": list(task_ids),
        "pending": list(task_ids), "sent_at": now.isoformat(),
    }
    cutoff = now - timedelta(days=RETENTION_DAYS)
    for bid in list(bundles):
        try:
            if datetime.fromisoformat(bundles[bid].get("sent_at", "")) < cutoff:
                bundles.pop(bid)
        except (TypeError, ValueError):
            bundles.pop(bid)


def bundle_for(state: dict, bundle_id: str) -> dict | None:
    entry = state.get("bundles", {}).get(bundle_id)
    return entry if isinstance(entry, dict) else None


def supersede(state: dict, bundle_id: str, task_ids: set[str]) -> bool:
    """這些任務要發新通知了：從舊合併訊息的待處理清單拿掉。回傳舊訊息是否已經沒有要留的任務（可以刪）。"""
    entry = bundle_for(state, bundle_id)
    if entry is None:
        return True
    entry["pending"] = [tid for tid in entry.get("pending", []) if tid not in task_ids]
    return not entry["pending"]
