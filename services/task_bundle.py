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
from services.repositories import COURSE_TASK_TYPE, TaskItem

MAX_BUNDLE = 25  # Discord 下拉選單最多 25 個選項
MESSAGE_LIMIT = 1900
RETENTION_DAYS = 45
KIND_DISPATCH = "dispatch"
KIND_ASSIGNMENT = "assignment"
KIND_BRIEFING = "briefing"  # 早報第一段：先確認還沒收到的任務（M3-29 第五版）
# 已交件（待審核）或修改中（需修改）的任務退回「尚未開始」會打亂審核流程，請藝人直接聯絡控方
CONCERN_STATUSES = {"尚未開始", "進行中", "待提交", "已逾期", None, ""}
# 已逾期的任務可以回報，但不退回、不取消 Discord 顯示：保留逾期紀錄，由控方決定延期或結案（2026-10-23 Johnny 選 A）
KEEP_STATUS_ON_CONCERN = {"已逾期"}
CONCERN_BLOCKED_REPLY = "這筆你已經交了（或正在修改），小律沒辦法用按鈕幫你轉達。有問題請直接找公司喔。"
ALREADY_REPORTED_REPLY = "這筆你已經跟小律說過了，公司調整好會再通知你。"

_TITLES = {KIND_DISPATCH: "📌 **任務通知**", KIND_ASSIGNMENT: "📚 **課後作業通知**"}
_FOOTERS = {
    KIND_DISPATCH: "沒問題的話按「✅ 全部收到」，小律會幫你告訴公司。哪一筆不行，從「⚠️ 哪一筆不行？」選出來，小律幫你轉達。想看內容用「看任務內容」。",
    KIND_ASSIGNMENT: "這幾筆現在可以交了！想看作業要求用「看任務內容」；看過後按「✅ 全部收到」。交件請用 `/我的任務` → 提交任務。",
}


def can_report_concern(task: TaskItem) -> bool:
    return task.status in CONCERN_STATUSES


def concern_keeps_status(task: TaskItem) -> bool:
    """回報問題後任務維持原狀態（不退回尚未開始）。"""
    return task.status in KEEP_STATUS_ON_CONCERN


def chunks(tasks: list[TaskItem]) -> list[list[TaskItem]]:
    return [tasks[i:i + MAX_BUNDLE] for i in range(0, len(tasks), MAX_BUNDLE)]


def _limit(text: str) -> str:
    if len(text) <= MESSAGE_LIMIT:
        return text
    return text[: MESSAGE_LIMIT - 30].rstrip() + "\n…（完整清單請用 `/我的任務`）"


def reported_item_line(task: TaskItem) -> str:
    return f"・~~{strip_notion_links(task.name)}~~｜⚠️ 小律已轉告公司，等調整"


def receive_label(pending: int, reported: int) -> str:
    """沒人回報問題時是「全部收到」；有回報過就改成「收下剩下 N 筆」（M3-28／M3-30）。"""
    return "全部收到" if not reported else f"收下剩下 {pending} 筆"


def greeting_line(greeting: dict | None) -> str:
    """「方晨同學，早安！我是小律。」＋小律今天的一句（M3-30）。沒有名字就只說早安。"""
    greeting = greeting or {}
    name = greeting.get("name") or ""
    head = f"☀️ **{name}同學，早安！我是小律。**" if name else "☀️ **早安！我是小律。**"
    opening = greeting.get("opening") or ""
    return f"{head}\n{opening}" if opening else head


def _briefing_first_text(tasks: list[TaskItem], deadline_text, reported, greeting: dict | None = None) -> str:
    """早報第一段（M3-29 第五版、M3-30 小律的口吻）：只問還沒收到的任務，不寫「新任務」。"""
    lines = [greeting_line(greeting), "", f"📌 **有 {len(tasks)} 筆任務還在等你按收到喔**"]
    for task in tasks:
        if task.page_id in reported:
            lines.append(reported_item_line(task))
            continue
        lines.append(f"・{strip_notion_links(task.name)}｜{deadline_text(task)}")
    sent_days = sorted({(t.discord_notified_at.month, t.discord_notified_at.day) for t in tasks if t.discord_notified_at})
    if len(sent_days) == 1:
        lines.append(f"（都是 {sent_days[0][0]}/{sent_days[0][1]} 派發的）")
    elif sent_days:
        lines.append(f"（{sent_days[0][0]}/{sent_days[0][1]}～{sent_days[-1][0]}/{sent_days[-1][1]} 派發的）")
    pending = sum(1 for task in tasks if task.page_id not in reported)
    lines.append("")
    if reported:
        lines.append(f"剩下 {pending} 筆沒問題的話，按「✅ {receive_label(pending, len(reported))}」就好。")
    else:
        lines.append("沒問題的話按「✅ 全部收到」，小律會幫你告訴公司 ✨")
        lines.append("哪一筆不行，從「⚠️ 哪一筆不行？」選出來，小律幫你轉達。")
    lines.append("收好之後，今天要注意的事會出現在下面。")
    return _limit("\n".join(lines))


def bundle_text(
    kind: str, tasks: list[TaskItem], deadline_text, reported: set[str] | frozenset = frozenset(),
    greeting: dict | None = None,
) -> str:
    if kind == KIND_BRIEFING:
        return _briefing_first_text(tasks, deadline_text, reported, greeting)
    title = _TITLES[kind]
    if kind == KIND_ASSIGNMENT and any(task.task_type != COURSE_TASK_TYPE for task in tasks):
        title = "📚 **作業開放通知**"  # 有非課程的任務就不叫「課後作業」（M2-15）
    lines = [f"{title}（{len(tasks)}）"]
    for task in tasks:
        if task.page_id in reported:
            lines.append(reported_item_line(task))
        else:
            lines.append(f"・{strip_notion_links(task.name)}｜{deadline_text(task)}")
    pending = sum(1 for task in tasks if task.page_id not in reported)
    if reported and pending:
        footer = f"剩下 {pending} 筆沒問題的話，按「✅ {receive_label(pending, len(reported))}」就好。哪一筆也不行，一樣從「⚠️ 哪一筆不行？」選。"
    else:
        footer = _FOOTERS[kind]
    lines += ["", footer]
    return _limit("\n".join(lines))


def done_text(received: list[TaskItem], reported: list[TaskItem], at: datetime, deadline_text) -> str:
    """合併訊息每一筆都處理完了（收到或回報問題）：收成一行＋清單（M3-27／M3-28）。"""
    head = []
    if received:
        head.append(f"✅ {len(received)} 筆任務收好了，小律記下來囉")
    if reported:
        head.append(f"⚠️ {len(reported)} 筆小律已轉告公司")
    lines = ["　".join(head) + f"｜{at:%m/%d %H:%M}"]
    lines += [f"・{strip_notion_links(task.name)}（{deadline_text(task)}）" for task in received]
    lines += [reported_item_line(task) for task in reported]
    if received:
        lines.append("收好之後才發現哪一筆不行，一樣從下面「⚠️ 哪一筆不行？」告訴小律。")
    return _limit("\n".join(lines))


def reported_card_line(task: TaskItem, at: datetime) -> str:
    """單筆卡回報問題後收成的那一行（M3-28）。"""
    return f"⚠️ 小律已轉告公司｜{strip_notion_links(task.name)}｜等調整｜{at:%m/%d %H:%M}"


def staff_bundle_text(tasks: list[TaskItem], artist_names, deadline_text) -> str:
    """相關同仁的合併知會（沒有按鈕）。artist_names(task) 回傳這筆的藝人名稱清單。"""
    lines = [f"👥 **同仁知會（{len(tasks)}）**：你是這些任務的相關同仁，這則僅供通知，不需要按任何按鈕。"]
    for task in tasks:
        names = "、".join(artist_names(task))
        lines.append(f"・{strip_notion_links(task.name)}｜{deadline_text(task)}" + (f"｜藝人：{names}" if names else ""))
    return _limit("\n".join(lines))


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


def mark_reported(state: dict, bundle_id: str, task_id: str) -> dict | None:
    """這筆在合併訊息裡回報了問題：記下來，之後重畫訊息時劃掉它（M3-28）。"""
    entry = bundle_for(state, bundle_id)
    if entry is None:
        return None
    reported = list(entry.get("reported") or [])
    if task_id not in reported:
        reported.append(task_id)
    entry["reported"] = reported
    return entry


def supersede(state: dict, bundle_id: str, task_ids: set[str]) -> bool:
    """這些任務要發新通知了：從舊合併訊息的待處理清單拿掉。回傳舊訊息是否已經沒有要留的任務（可以刪）。"""
    entry = bundle_for(state, bundle_id)
    if entry is None:
        return True
    entry["pending"] = [tid for tid in entry.get("pending", []) if tid not in task_ids]
    return not entry["pending"]
