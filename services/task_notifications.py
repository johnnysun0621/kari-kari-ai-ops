"""任務派發通知的純邏輯與本機去重狀態。"""
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from services import notion_links
from services.repositories import TaskItem

STATE_PATH = Path(".state/task_dispatch_notifications.json")
ASSIGNMENT_STATE_PATH = Path(".state/task_assignment_notifications.json")
# 提交方式已全部停用（M4-03）：所有交件一律上傳到 Bot 提供的公司 Drive 資料夾。
SUBMIT_GUIDE = "需要交件時，請用 `/我的任務` → 提交任務，檔案上傳到 Bot 提供的公司 Drive 資料夾。"
STAFF_HEADER = "👥 同仁知會：你是這個任務的相關同仁，這則僅供通知，不需要按收到。"


def staff_message(artist_content: str) -> str:
    """M6-05：發給相關同仁的版本。沿用藝人那份內容（同樣不含 Notion 網址），
    拿掉只對藝人有意義的交件指引——同仁不在 Artists，/我的任務 查不到這筆。"""
    body = artist_content.replace(SUBMIT_GUIDE, "").strip()
    return f"{STAFF_HEADER}\n\n{body}"[:2000]


def format_reminder_interval(minutes: float) -> str:
    """把 REMINDER_INTERVAL_MINUTES 轉成給人看的文字；避免訊息寫死跟設定值脫節。"""
    if minutes < 1:
        return f"{round(minutes * 60)} 秒"
    return f"{minutes:g} 分鐘"


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
    return (
        f"📌 **新任務通知**\n"
        f"任務：**{task.name}**\n"
        f"類型：{task.task_type or '任務'}\n"
        f"{deadline_line}"
        f"{extra_text}\n\n"
        f"**任務內容**\n{description}\n\n"
        f"請先按「收到任務」確認你已看到這項任務。{SUBMIT_GUIDE}"
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
    assignment_deadline = task.submission_deadline
    assignment_has_time = task.submission_deadline_has_time
    if assignment_deadline is None and task.open_for_submission:
        # 舊資料相容：尚未新增獨立提交期限時，暫時沿用原截止日。
        assignment_deadline = task.end
        assignment_has_time = task.end_has_time
    if assignment_deadline:
        deadline = assignment_deadline.strftime(
            "%m/%d %H:%M" if assignment_has_time else "%m/%d"
        )
    return (
        f"📚 **課後作業通知**\n"
        f"任務：**{task.name}**\n"
        f"截止：{deadline}\n\n"
        f"**作業要求**\n{description}\n\n"
        f"請先按「收到任務」確認你已看到這項作業。{SUBMIT_GUIDE}"
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
    """移除控方排程欄位，保留真正給藝人的任務說明。

    控方常把參考頁直接貼進任務說明；藝人拿到任何 Notion 網址都能往上翻到整個 Tasks（M7-04），
    所以這裡一併清掉 Notion 連結（其他網址照留）。
    """
    kept: list[str] = []
    for line in notion_links.strip_notion_links(description or "").splitlines():
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
    """產生公開公告；不放入藝人私訊用的提交按鈕與私人內容。

    公告區藝人看得到，所以不附 Notion 連結——任務頁往上翻就是整個 Tasks。
    """
    time_text = "時間待定"
    if task.start and task.end:
        time_text = f"{task.start:%m/%d %H:%M}–{task.end:%H:%M}"
    return (
        f"📢 **{task.task_type or '正式安排'}公告**\n"
        f"任務：**{task.name}**\n"
        f"時間：{time_text}"
    ).strip()


# ---------- 截止前提醒（T-5／當天）與藝人確認 ----------

# Discord custom_id 上限 100 字且要能被 regex 抓，所以階段用短代碼；
# 中文階段名只在給人看的地方出現。
REMINDER_STAGE_CODES = {"5 天前": "d5", "3 天前": "d3", "1 天前": "d1", "當天": "d0"}
_STAGE_BY_CODE = {code: stage for stage, code in REMINDER_STAGE_CODES.items()}
REMINDER_SENT_MARK = "已發送給"


def reminder_stage_code(stage: str) -> str | None:
    """「當天」→ "d0"；逾期沒有確認按鈕，回 None。"""
    return REMINDER_STAGE_CODES.get(stage)


def reminder_stage_from_code(code: str) -> str | None:
    return _STAGE_BY_CODE.get(code)


def reminder_message(task: TaskItem, stage: str, deadline_text: str) -> str:
    """截止前提醒的私訊內容。

    藝人手上已經沒有 Notion 連結（藝人端零 Notion 入口），所以提醒必須自帶任務說明
    ，不能只丟一個任務名稱。deadline_text 由呼叫端用 ui_text 產生，
    這裡不重算，避免兩邊的「交件／截止」稱呼不一致。
    """
    description = _artist_facing_description(task.description)
    if len(description) > 700:
        description = description[:697].rstrip() + "..."
    lines = [f"⏰ **任務提醒（{stage}）**", f"任務：**{task.name}**", deadline_text]
    if description:
        lines.append(f"\n**任務內容**\n{description}")
    lines.append("\n看到這則提醒請按下方「收到通知」，讓控方知道你已掌握進度。")
    return "\n".join(lines)


def format_reminder_ack_line(stage: str, when: datetime, artist_name: str) -> str:
    """寫進 Notion「提醒確認」的一行：`當天｜09/25 09:12｜Johnny`。"""
    return f"{stage}｜{when:%m/%d %H:%M}｜{artist_name}"


def format_reminder_sent_line(stage: str, when: datetime, count: int) -> str:
    """提醒送出時也記一行，讓控方分得出「還沒發」和「發了沒人按」。"""
    return f"{stage}｜{when:%m/%d %H:%M}｜{REMINDER_SENT_MARK} {count} 人"


def acknowledged_artists(reminder_ack: str, stage: str) -> set[str]:
    """從「提醒確認」文字撈出某個階段已確認的藝人名字（不含「已發送給」那行）。"""
    names: set[str] = set()
    for line in reminder_ack.splitlines():
        parts = [part.strip() for part in line.split("｜")]
        if len(parts) != 3 or parts[0] != stage:
            continue
        if parts[2].startswith(REMINDER_SENT_MARK):
            continue
        names.add(parts[2])
    return names


def reminder_ack_summary(task: TaskItem, artist_names: list[str]) -> str:
    """給 /任務總覽 用的一行：`提醒確認：1/3 ✓（Johnny）`；沒發過提醒就回空字串。"""
    if not task.reminder_ack.strip():
        return ""
    latest_stage = None
    latest_day = ""
    for line in task.reminder_ack.splitlines():
        parts = [part.strip() for part in line.split("｜")]
        if len(parts) == 3:
            latest_stage, latest_day = parts[0], parts[1][:5]
    if latest_stage is None:
        return ""
    ack_text = task.reminder_ack
    if latest_stage == "早報":
        # 每日早報每天的階段名稱都是「早報」，只算最近那天的，昨天按過的不算今天看過（M3-24）
        ack_text = "\n".join(
            line for line in task.reminder_ack.splitlines()
            if len(line.split("｜")) == 3 and line.split("｜")[1].strip().startswith(latest_day)
        )
        latest_stage = f"{latest_day} 早報"
        confirmed = acknowledged_artists(ack_text, "早報")
    else:
        confirmed = acknowledged_artists(ack_text, latest_stage)
    ordered = [name for name in artist_names if name in confirmed]
    total = len(artist_names) or len(confirmed)
    names_text = f"（{'、'.join(ordered)}）" if ordered else ""
    return f"提醒確認（{latest_stage}）：{len(ordered)}/{total} ✓{names_text}"


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
    """管理端重送清單：所有仍在進行的可見任務。

    刻意**包含**藝人已按過「收到任務」的任務——控方改了時間或內容後需要重發，
    這是正常營運需求。排除已確認的話，控方只能手動清空 Notion 的兩個時間欄位
    才叫得出來，那是很糟的工作流。

    重送時 `reset_task_discord_notification` 會清掉通知與收到時間，
    所以藝人會被要求重新確認一次。
    """
    return (
        task.discord_visible
        and task.status not in {"已完成", "已取消", "已封存"}
    )


def sort_resendable_tasks(tasks: list[TaskItem]) -> list[TaskItem]:
    """未確認的排前面——那是最常見的重送對象；Discord 下拉最多只放得下 25 個。"""
    return sorted(
        tasks,
        key=lambda task: (task.discord_acknowledged_at is not None, task.name or ""),
    )


def filter_tasks_by_keyword(tasks: list[TaskItem], keyword: str | None) -> list[TaskItem]:
    """依任務名稱過濾（不分大小寫、比對片段）。

    進行中的任務一多，25 個下拉選項就不夠用；控方要重送時通常已經知道是哪一筆，
    用關鍵字直接縮小範圍比翻頁實際。
    """
    if not keyword or not keyword.strip():
        return tasks
    needle = keyword.strip().casefold()
    return [task for task in tasks if needle in (task.name or "").casefold()]


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


PUBLISH_DIGEST_STATE_PATH = Path(".state/publish_confirm_digest.json")
PUBLISH_DIGEST_MAX_LINES = 20


def publish_confirm_digest_due(
    now: datetime, last_sent_on: str | None, weekday: int, hour: int
) -> bool:
    """今天是不是該發「待確認發布」彙整：指定星期幾（-1＝每天）、過了指定整點、今天還沒發。

    last_sent_on 是持久化的最後發送日期，重啟後不會同一天重複發。
    """
    if weekday >= 0 and now.weekday() != weekday:
        return False
    if now.hour < hour:
        return False
    return last_sent_on != now.date().isoformat()


def publish_confirm_digest_message(
    tasks: list[TaskItem], artist_names: dict[str, str], after_days: int
) -> str:
    """彙整成一則訊息；只放任務名稱、藝人與截止日，不放 Notion 網址。"""
    lines = [
        f"📋 **發文待確認發布**（{len(tasks)} 筆）",
        f"以下發文任務審核已通過，但截止日過了 {after_days} 天仍停在「已完成」。",
        "已確認實際發布的，請回 Notion 把狀態改成「已完成發文」。",
        "",
    ]
    for task in tasks[:PUBLISH_DIGEST_MAX_LINES]:
        names = "、".join(
            artist_names[page_id] for page_id in task.artist_page_ids if page_id in artist_names
        )
        due = f"｜截止 {task.end.strftime('%m/%d')}" if task.end else ""
        lines.append(f"• {task.name[:60]}" + (f"｜{names}" if names else "") + due)
    hidden = len(tasks) - PUBLISH_DIGEST_MAX_LINES
    if hidden > 0:
        lines.append(f"…另有 {hidden} 筆，請到 Notion 篩選「發文類＋已完成」查看。")
    return "\n".join(lines)


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
