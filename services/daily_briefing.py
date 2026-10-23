"""每日早報（M3-24，Johnny 2026-10-20 定案方案 B）。

藝人私訊原本一個任務會收到派發、每 12 小時重發、5／3／1 天前提醒各一則，同一輪還會一次跳好幾個
階段（近 8 天：每天約 20 則提醒、25% 同時多階段、SOMA 51 則派發沒按收到）。改成：

- 每位藝人每天 DAILY_BRIEFING_HOUR（預設 9）點一則早報：已逾期、需修改、快到期
  （還沒按收到的不列進早報，改成把任務卡搬到早報下面，M3-29）
- 底下一個「看某一筆的詳細」下拉選單＋一顆「👌 我都看到了」；按了寫進每筆任務的「提醒確認」，早報收成一行
- 隔天發新早報時刪掉前一天的，私訊裡永遠只有今天這則
- 已逾期、需修改確認過後隔天仍列出，標「⚠️ 仍未完成（某天已確認）」，直到處理好
- 快到期只在提醒點（5／3／1 天前、當天）出現：截止前提醒的 loop 把到期階段排進佇列，早報取用後清掉

這裡是純函式＋.state 讀寫；Discord 元件與發送在 cogs/briefing.py。
"""
from dataclasses import dataclass
from datetime import date, datetime

import config
from services import repositories, task_notifications
from services.notion_links import strip_notion_links

SECTION_OVERDUE = "overdue"
SECTION_NEEDS_FIX = "needs_fix"
SECTION_DUE_SOON = "due_soon"
SECTION_UNRECEIVED = "unreceived"
SECTION_AWAITING = "awaiting"  # M2-14：不用交件的任務過了截止，等公司確認
AWAITING_NOTE = "還沒做完請盡快完成；已經做完不用理會，公司會確認。"
SECTION_TITLES = {
    SECTION_OVERDUE: "🔴 **已逾期**",
    SECTION_NEEDS_FIX: "🛠 **需修改**",
    SECTION_AWAITING: "⏳ **已過截止，等公司確認**",
    SECTION_DUE_SOON: "⏰ **快到期**",
    SECTION_UNRECEIVED: "📌 **還沒按收到**",
}
SECTION_ORDER = (SECTION_OVERDUE, SECTION_NEEDS_FIX, SECTION_AWAITING, SECTION_DUE_SOON, SECTION_UNRECEIVED)
SECTION_SHORT = {
    SECTION_OVERDUE: "已逾期", SECTION_NEEDS_FIX: "需修改", SECTION_AWAITING: "已過截止，等公司確認",
    SECTION_DUE_SOON: "快到期", SECTION_UNRECEIVED: "還沒按收到",
}
MAX_ITEMS = 25  # Discord 下拉選單最多 25 個選項
MESSAGE_LIMIT = 1900
ACK_STAGE = "早報"


@dataclass(frozen=True)
class BriefingItem:
    task: repositories.TaskItem
    section: str
    acked_on: str | None = None  # 以前按過「我都看到了」的日期（只對逾期／需修改有意義）


def briefing_due(now: datetime, last_date: str | None, hour: int) -> bool:
    return now.hour >= hour and last_date != now.date().isoformat()


def _section_for(task: repositories.TaskItem, queued: set[str], now: datetime) -> str | None:
    if task.status in repositories.INACTIVE_TASK_STATUSES or task.status == "待審核":
        return None
    if task.status == "已逾期":
        return SECTION_OVERDUE
    if task.status == "需修改":
        return SECTION_NEEDS_FIX
    if repositories.task_awaits_ops_confirmation(task, now):
        return SECTION_AWAITING
    if task.page_id in queued:
        return SECTION_DUE_SOON
    if task.discord_acknowledged_at is None and task_notifications.can_acknowledge(task):
        return SECTION_UNRECEIVED
    return None


def build_items(
    artist_page_id: str,
    tasks: list[repositories.TaskItem],
    queued_task_ids: set[str],
    acks: dict,
    now: datetime | None = None,
) -> list[BriefingItem]:
    """這位藝人今天早報要列的任務，依區塊排序、每筆只出現一次，最多 MAX_ITEMS 筆。"""
    now = now or datetime.now(config.TZ)
    items: list[BriefingItem] = []
    for task in tasks:
        if not task.discord_visible or artist_page_id not in task.artist_page_ids:
            continue
        section = _section_for(task, queued_task_ids, now)
        if section is None:
            continue
        acked_on = (acks.get(task.page_id) or {}).get(artist_page_id)
        items.append(BriefingItem(task, section, acked_on if section in (SECTION_OVERDUE, SECTION_NEEDS_FIX) else None))

    def sort_key(item: BriefingItem):
        deadline, _ = repositories.task_action_deadline(item.task)
        return (SECTION_ORDER.index(item.section), deadline is None, deadline.timestamp() if deadline else 0)

    return sorted(items, key=sort_key)[:MAX_ITEMS]


def split_unreceived(items: list[BriefingItem]) -> tuple[list[BriefingItem], list[repositories.TaskItem]]:
    """還沒按收到的任務不放進早報（M3-29，Johnny 2026-10-23 選戊）。

    早報只放「知道就好」的提醒；要藝人做決定的新任務放在早報裡，他會以為按「我都看到了」就算收到。
    改成把那張任務卡搬到早報下面（cogs/briefing.py），收到永遠只在任務卡上按。
    回傳（早報要列的、還沒按收到的任務）。
    """
    keep = [item for item in items if item.section != SECTION_UNRECEIVED]
    unreceived = [item.task for item in items if item.section == SECTION_UNRECEIVED]
    return keep, unreceived


def needs_card_refresh(task: repositories.TaskItem, today: date) -> bool:
    """今天之前派發、還沒按收到的任務卡：今天早上搬到最下面。今天才派發的就不用再送一次。"""
    notified = task.discord_notified_at
    return notified is None or notified.date() < today


def unreceived_tip(count: int) -> str:
    return f"📌 你還有 {count} 筆任務還沒按收到，任務卡在這則下面 👇"


def build_staff_items(
    person_page_id: str,
    tasks: list[repositories.TaskItem],
    queued_task_ids: set[str],
    now: datetime | None = None,
) -> list[BriefingItem]:
    """相關同仁（M6-05）的早報：跟自己有關的已逾期、需修改、已過截止等確認、快到期；同仁不用按收到，所以沒有那一區。"""
    now = now or datetime.now(config.TZ)
    items = []
    for task in tasks:
        if not task.discord_visible or person_page_id not in task.staff_page_ids:
            continue
        section = _section_for(task, queued_task_ids, now)
        if section in (None, SECTION_UNRECEIVED):
            continue
        items.append(BriefingItem(task, section))

    def sort_key(item: BriefingItem):
        deadline, _ = repositories.task_action_deadline(item.task)
        return (SECTION_ORDER.index(item.section), deadline is None, deadline.timestamp() if deadline else 0)

    return sorted(items, key=sort_key)[:MAX_ITEMS]


STAFF_FOOTER = "你是這些任務的相關同仁，這則只是讓你知道進度。想看某一筆的內容，用下方選單。"


def _acked_tag(acked_on: str | None, today: date) -> str:
    if not acked_on or acked_on == today.isoformat():
        return ""
    try:
        day = date.fromisoformat(acked_on)
    except ValueError:
        return ""
    return f"　⚠️ 仍未完成（{day.month}/{day.day} 已確認）"


def briefing_text(
    day: date,
    items: list[BriefingItem],
    deadline_text,
    extra_count: int = 0,
    footer: str | None = None,
    unreceived_count: int = 0,
) -> str:
    """早報內文。deadline_text 由呼叫端傳入（cogs/ui_text.deadline_text），這裡不 import cogs。"""
    weekdays = "一二三四五六日"
    lines = [f"☀️ **{day.month}/{day.day}（{weekdays[day.weekday()]}）今天的任務提醒**"]
    if unreceived_count:
        lines.append(unreceived_tip(unreceived_count))
    for section in SECTION_ORDER:
        section_items = [item for item in items if item.section == section]
        if not section_items:
            continue
        lines.append("")
        lines.append(f"{SECTION_TITLES[section]}（{len(section_items)}）")
        for item in section_items:
            name = strip_notion_links(item.task.name)
            lines.append(f"・{name}｜{deadline_text(item.task)}{_acked_tag(item.acked_on, day)}")
        if section == SECTION_AWAITING:
            lines.append(AWAITING_NOTE)
    if extra_count:
        lines.append(f"\n（另有 {extra_count} 筆，完整清單請用 `/我的任務`）")
    lines.append("\n" + (footer or "想看某一筆的內容，用下方選單；看完按「👌 我都看到了」。要交件請用 `/我的任務`。"))
    text = "\n".join(lines)
    if len(text) > MESSAGE_LIMIT:
        text = text[: MESSAGE_LIMIT - 30].rstrip() + "\n…（太長了，完整清單請用 `/我的任務`）"
    return text


def collapsed_text(day: date, count: int, acked_at: datetime, reported: int = 0) -> str:
    text = f"☀️ {day.month}/{day.day} 今天的任務提醒（{count} 筆）\n✅ 已讀（{acked_at:%H:%M}）"
    if reported:
        text += f"　⚠️ 已回報 {reported} 筆"
    return text


def mark_reported_line(text: str, task_name: str) -> str:
    """早報裡回報了問題的那一筆：劃掉並標「已回報」，其他行不動（M3-28）。"""
    name = strip_notion_links(task_name)
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.startswith(f"・{name}｜"):
            lines[i] = f"・~~{name}~~｜⚠️ 已回報問題，等控方調整"
    return "\n".join(lines)


def option_description(item: BriefingItem, deadline_text) -> str:
    return f"{SECTION_SHORT[item.section]}｜{deadline_text(item.task)}"[:100]


# ---------- .state（notification_log 的共用 state） ----------

def queue_stages(state: dict, task_id: str, stages: list[str], now: datetime) -> None:
    """截止前提醒到期的階段排進早報佇列（不再一則一則私訊藝人）。"""
    queue = state.setdefault("briefing_queue", {})
    entry = queue.setdefault(task_id, {"stages": [], "queued_at": now.isoformat()})
    for stage in stages:
        if stage not in entry["stages"]:
            entry["stages"].append(stage)


def queued_task_ids(state: dict) -> set[str]:
    return set(state.get("briefing_queue", {}))


def clear_queue(state: dict) -> None:
    state["briefing_queue"] = {}


def remember_briefing(state: dict, user_id: int | str, entry: dict) -> None:
    state.setdefault("briefings", {})[str(user_id)] = entry


def briefing_for(state: dict, user_id: int | str) -> dict | None:
    entry = state.get("briefings", {}).get(str(user_id))
    return entry if isinstance(entry, dict) else None


def record_ack(state: dict, task_ids: list[str], artist_page_id: str, day: date) -> None:
    acks = state.setdefault("briefing_acks", {})
    for task_id in task_ids:
        acks.setdefault(task_id, {})[artist_page_id] = day.isoformat()


def prune_acks(state: dict, active_task_ids: set[str]) -> None:
    """任務結案後不再需要「仍未完成（已確認）」的標記。"""
    acks = state.get("briefing_acks", {})
    for task_id in list(acks):
        if task_id not in active_task_ids:
            acks.pop(task_id)
