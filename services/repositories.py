"""五張 P0 資料庫的 CRUD。欄位名稱 → Notion API payload 的唯一對應處。

Notion 欄位名稱是中文（與正式資料庫完全一致），若 Notion 端改名，只需改這裡。
"""
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta

import config
from services import notion_api, overlap, time_collection
from services.notion_api import NotionAPIError

# ---------- property payload builders ----------

def _title(text: str) -> dict:
    return {"title": [{"text": {"content": text[:2000]}}]}


def _rich_text(text: str) -> dict:
    return {"rich_text": [{"text": {"content": text[:2000]}}]}


def _select(name: str) -> dict:
    return {"select": {"name": name}}


def _number(value: float | int) -> dict:
    return {"number": value}


def _checkbox(value: bool) -> dict:
    return {"checkbox": value}


def _url(value: str) -> dict:
    return {"url": value}


def _date(start: datetime, end: datetime | None = None) -> dict:
    d: dict = {"start": start.isoformat()}
    if end is not None:
        d["end"] = end.isoformat()
    return {"date": d}


def _relation(page_ids: list[str]) -> dict:
    return {"relation": [{"id": pid} for pid in page_ids]}


# ---------- property readers ----------

def _read_title(page: dict, name: str) -> str:
    items = page["properties"].get(name, {}).get("title", [])
    return "".join(i.get("plain_text", "") for i in items)


def _read_rich_text(page: dict, name: str) -> str:
    items = page["properties"].get(name, {}).get("rich_text", [])
    return "".join(i.get("plain_text", "") for i in items)


def _read_select(page: dict, name: str) -> str | None:
    sel = page["properties"].get(name, {}).get("select")
    return sel["name"] if sel else None


def _read_number(page: dict, name: str) -> float | None:
    return page["properties"].get(name, {}).get("number")


def _read_checkbox(page: dict, name: str) -> bool:
    return bool(page["properties"].get(name, {}).get("checkbox"))


def _read_first_checkbox(page: dict, names: list[str]) -> bool:
    for name in names:
        if name in page.get("properties", {}):
            return _read_checkbox(page, name)
    return False


def _read_date_start(page: dict, name: str) -> datetime | None:
    d = page["properties"].get(name, {}).get("date")
    if not d or not d.get("start"):
        return None
    dt = datetime.fromisoformat(d["start"])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.TZ)
    return dt.astimezone(config.TZ)


def _read_date_end(page: dict, name: str) -> datetime | None:
    d = page["properties"].get(name, {}).get("date")
    if not d or not d.get("end"):
        return None
    dt = datetime.fromisoformat(d["end"])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.TZ)
    return dt.astimezone(config.TZ)


@dataclass(frozen=True)
class DateValue:
    value: datetime | None
    has_time: bool = False


def _parse_notion_datetime(raw: str | None) -> DateValue:
    if not raw:
        return DateValue(None, False)
    has_time = "T" in raw
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.TZ)
    return DateValue(dt.astimezone(config.TZ), has_time)


def _read_date_value(page: dict, name: str, key: str = "start") -> DateValue:
    d = page["properties"].get(name, {}).get("date")
    if not d:
        return DateValue(None, False)
    return _parse_notion_datetime(d.get(key))


def _read_relation_ids(page: dict, name: str) -> list[str]:
    return [r["id"] for r in page["properties"].get(name, {}).get("relation", [])]


def _read_url(page: dict, name: str) -> str:
    return page["properties"].get(name, {}).get("url") or ""


def _read_multi_select(page: dict, name: str) -> list[str]:
    items = page["properties"].get(name, {}).get("multi_select", [])
    return [i["name"] for i in items]


def _read_unique_id(page: dict, name: str) -> int | None:
    uid = page["properties"].get(name, {}).get("unique_id")
    return uid.get("number") if uid else None


# ---------- Artists ----------

@dataclass
class Artist:
    page_id: str
    name: str
    discord_user_id: str
    status: str | None = None  # KariKari schema: 合約狀態


def _to_artist(page: dict) -> Artist:
    return Artist(
        page_id=page["id"],
        name=_read_title(page, "藝人名稱"),
        discord_user_id=_read_rich_text(page, "Discord User ID").strip(),
        status=_read_select(page, "合約狀態"),
    )


INACTIVE_ARTIST_STATUSES = {"暫停", "封存"}


def filter_schedulable_artists(artists: list[Artist]) -> list[Artist]:
    """Return artists eligible for Discord scheduling in the KariKari schema."""
    return [
        artist for artist in artists
        if artist.discord_user_id and artist.status not in INACTIVE_ARTIST_STATUSES
    ]


async def get_active_artists() -> list[Artist]:
    pages = await notion_api.query_data_source(config.DS_ARTISTS)
    return filter_schedulable_artists(list(map(_to_artist, pages)))


async def get_artists_by_ids(page_ids: list[str]) -> list[Artist]:
    """依 relation page id 取藝人（撈 Active 全表後過濾，P0 資料量小）。"""
    pages = await notion_api.query_data_source(config.DS_ARTISTS)
    wanted = set(page_ids)
    return [a for a in map(_to_artist, pages) if a.page_id in wanted]


async def get_artist_map() -> dict[str, Artist]:
    """一次撈全表回 {page_id: Artist}，供批次比對（避免逐筆重撈整張藝人表）。"""
    pages = await notion_api.query_data_source(config.DS_ARTISTS)
    return {a.page_id: a for a in map(_to_artist, pages)}


async def find_artist_by_discord_id(discord_user_id: int | str) -> Artist | None:
    pages = await notion_api.query_data_source(
        config.DS_ARTISTS,
        filter_={"property": "Discord User ID", "rich_text": {"equals": str(discord_user_id)}},
    )
    return _to_artist(pages[0]) if pages else None


# ---------- Schedule Requests ----------

@dataclass
class ScheduleRequest:
    page_id: str
    url: str
    name: str
    course_type: str | None
    status: str | None
    duration_minutes: int
    artist_page_ids: list[str] = field(default_factory=list)
    fill_deadline: datetime | None = None
    candidates_text: str = ""
    teacher: str = ""
    created_time: datetime | None = None
    sla_breached: bool = False
    schedule_id: int | None = None


def _to_request(page: dict) -> ScheduleRequest:
    created = datetime.fromisoformat(page["created_time"].replace("Z", "+00:00"))
    return ScheduleRequest(
        page_id=page["id"],
        url=page.get("url", ""),
        name=_read_title(page, "排課名稱"),
        course_type=_read_select(page, "課程類型"),
        status=_read_select(page, "需求狀態"),
        duration_minutes=int(_read_number(page, "時長分鐘") or 60),
        artist_page_ids=_read_relation_ids(page, "參與藝人"),
        fill_deadline=_read_date_start(page, "填寫截止時間"),
        candidates_text=_read_rich_text(page, "候選時段"),
        teacher=_read_rich_text(page, "老師 / 確認對象"),
        created_time=created.astimezone(config.TZ),
        sla_breached=_read_checkbox(page, "是否超過 2 天 SLA"),
        schedule_id=_read_unique_id(page, "Schedule ID"),
    )


ACTIONABLE_REQUEST_STATUSES = ["等待藝人填寫", "比對中", "等待老師確認", "已確認"]
COMPLETED_REQUEST_STATUSES = ["已發布通告", "已取消"]
OPEN_REQUEST_STATUSES = ACTIONABLE_REQUEST_STATUSES


async def create_schedule_request(
    name: str,
    course_type: str,
    duration_minutes: int,
    artist_page_ids: list[str],
    fill_deadline: datetime,
    teacher: str,
    month_week: str = "",
    note: str = "",
) -> ScheduleRequest:
    props = {
        "排課名稱": _title(name),
        "課程類型": _select(course_type),
        "需求狀態": _select("等待藝人填寫"),
        "時長分鐘": _number(duration_minutes),
        "參與藝人": _relation(artist_page_ids),
        "填寫截止時間": _date(fill_deadline),
        "老師 / 確認對象": _rich_text(teacher),
    }
    if month_week:
        props["月份 / 週次"] = _rich_text(month_week)
    if note:
        props["備註"] = _rich_text(note)
    page = await notion_api.create_page(config.DS_SCHEDULE_REQUESTS, props)
    return _to_request(page)


async def get_request(page_id: str) -> ScheduleRequest | None:
    try:
        page = await notion_api.retrieve_page(page_id)
    except notion_api.NotionAPIError as e:
        if e.status_code == 404:
            return None  # 需求已不存在
        raise
    return _to_request(page)


async def get_requests_by_status(statuses: list[str]) -> list[ScheduleRequest]:
    filter_ = {"or": [{"property": "需求狀態", "select": {"equals": s}} for s in statuses]}
    pages = await notion_api.query_data_source(config.DS_SCHEDULE_REQUESTS, filter_=filter_)
    return [_to_request(p) for p in pages]


async def update_request_status(page_id: str, status: str) -> None:
    await notion_api.update_page(page_id, {"需求狀態": _select(status)})


async def set_request_candidates(page_id: str, candidates_text: str, status: str = "等待老師確認") -> None:
    await notion_api.update_page(page_id, {
        "候選時段": _rich_text(candidates_text),
        "需求狀態": _select(status),
    })


async def set_request_confirmed(
    page_id: str,
    final_start: datetime,
    final_end: datetime,
    session_page_id: str,
    notice_url: str = "",
) -> None:
    props = {
        "最終確認時段": _date(final_start, final_end),
        "需求狀態": _select("已發布通告"),
        "正式課程": _relation([session_page_id]),
    }
    if notice_url:
        props["正式通告 URL"] = _url(notice_url)
    await notion_api.update_page(page_id, props)


async def mark_request_sla_breached(page_id: str) -> None:
    await notion_api.update_page(page_id, {"是否超過 2 天 SLA": _checkbox(True)})


# ---------- Tasks / Projects formal schedule impact ----------

ACTIVE_TASK_STATUSES = {"尚未開始", "進行中", "待提交", "待審核", "需修改", "已逾期"}
INACTIVE_TASK_STATUSES = {"已完成", "已取消", "已封存"}
HARD_BLOCK_TASK_TYPES = {"課程場次", "錄音", "演出", "會議溝通"}
INACTIVE_PROJECT_STATUSES = {"已完成", "已取消", "已封存"}


@dataclass
class ProjectItem:
    page_id: str
    name: str
    status: str | None
    artist_page_ids: list[str] = field(default_factory=list)
    start: datetime | None = None
    end: datetime | None = None
    start_has_time: bool = False
    end_has_time: bool = False


@dataclass
class TaskItem:
    page_id: str
    name: str
    status: str | None
    task_type: str | None
    artist_page_ids: list[str] = field(default_factory=list)
    project_page_ids: list[str] = field(default_factory=list)
    start: datetime | None = None
    end: datetime | None = None
    start_has_time: bool = False
    end_has_time: bool = False
    # P0-2 任務查詢顯示用
    url: str = ""
    description: str = ""
    submit_method: str | None = None
    open_for_submission: bool = False
    latest_submission_url: str = ""
    task_id: int | None = None
    # P0-4 任務提醒用
    discord_visible: bool = False
    reminder_rules: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BusyInterval:
    source_id: str
    source_name: str
    source_type: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class ScheduleConflict:
    source_id: str
    source_name: str
    source_type: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class ScheduleWarning:
    source_id: str
    source_name: str
    source_type: str
    start: datetime | None = None
    end: datetime | None = None


@dataclass
class ScheduleImpactContext:
    busy_intervals: list[BusyInterval] = field(default_factory=list)
    warnings: list[ScheduleWarning] = field(default_factory=list)


def _to_project(page: dict) -> ProjectItem:
    start = _read_date_value(page, "開案日")
    end = _read_date_value(page, "截止日")
    return ProjectItem(
        page_id=page["id"],
        name=_read_title(page, "專案名稱"),
        status=_read_select(page, "專案狀態"),
        artist_page_ids=_read_relation_ids(page, "關聯藝人"),
        start=start.value,
        end=end.value,
        start_has_time=start.has_time,
        end_has_time=end.has_time,
    )


def _to_task(page: dict) -> TaskItem:
    start = _read_date_value(page, "開始日")
    end = _read_date_value(page, "截止日")
    return TaskItem(
        page_id=page["id"],
        name=_read_title(page, "任務名稱"),
        status=_read_select(page, "任務狀態"),
        task_type=_read_select(page, "任務類型"),
        artist_page_ids=_read_relation_ids(page, "關聯藝人"),
        project_page_ids=_read_relation_ids(page, "關聯專案"),
        start=start.value,
        end=end.value,
        start_has_time=start.has_time,
        end_has_time=end.has_time,
        url=page.get("url", ""),
        description=_read_rich_text(page, "任務說明"),
        submit_method=_read_select(page, "提交方式"),
        open_for_submission=_read_checkbox(page, "是否開放提交"),
        latest_submission_url=_read_url(page, "最新提交 URL"),
        task_id=_read_unique_id(page, "Task ID"),
        discord_visible=_task_discord_visible(page),
        reminder_rules=_read_multi_select(page, "提醒規則"),
    )


TASK_DISCORD_DISPLAY_PROPERTY = "Discord 顯示"
TASK_DISCORD_VISIBLE_LEGACY_PROPERTY = "Discord 可見"


def _task_discord_visible(page: dict) -> bool:
    return _read_first_checkbox(
        page,
        [TASK_DISCORD_DISPLAY_PROPERTY, TASK_DISCORD_VISIBLE_LEGACY_PROPERTY],
    )


def task_applies_to_artist(
    task: TaskItem,
    artist_page_id: str,
    project_map: dict[str, ProjectItem],
) -> bool:
    if artist_page_id in task.artist_page_ids:
        return True
    for project_page_id in task.project_page_ids:
        project = project_map.get(project_page_id)
        if project and artist_page_id in project.artist_page_ids:
            return True
    return False


def _is_active_task(task: TaskItem) -> bool:
    if task.status in INACTIVE_TASK_STATUSES:
        return False
    if task.status in ACTIVE_TASK_STATUSES:
        return True
    return task.status is None


def classify_task_time_impact(task: TaskItem) -> str | None:
    """Return 'busy', 'warning', or None for a Task's scheduling impact."""
    if not _is_active_task(task):
        return None
    has_any_date = bool(task.start or task.end)
    if (
        task.task_type in HARD_BLOCK_TASK_TYPES
        and task.start
        and task.end
        and task.start_has_time
        and task.end_has_time
        and task.end > task.start
    ):
        return "busy"
    return "warning" if has_any_date else None


def _date_span_for_warning(
    start: datetime | None,
    end: datetime | None,
) -> tuple[date, date] | None:
    if not start and not end:
        return None
    start_date = (start or end).date()
    end_date = (end or start).date()
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    return start_date, end_date


def _warning_overlaps_window(warning: ScheduleWarning, start: datetime, end: datetime) -> bool:
    span = _date_span_for_warning(warning.start, warning.end)
    if span is None:
        return False
    start_date, end_date = span
    return start.date() <= end_date and end.date() >= start_date


def _busy_overlaps_window(busy: BusyInterval, start: datetime, end: datetime) -> bool:
    return busy.start < end and start < busy.end


def busy_intervals_for_tasks(tasks: list[TaskItem]) -> list[BusyInterval]:
    intervals: list[BusyInterval] = []
    for task in tasks:
        if classify_task_time_impact(task) != "busy":
            continue
        intervals.append(
            BusyInterval(
                source_id=task.page_id,
                source_name=task.name,
                source_type="Task",
                start=task.start,
                end=task.end,
            )
        )
    return intervals


def warnings_for_tasks(tasks: list[TaskItem]) -> list[ScheduleWarning]:
    warnings: list[ScheduleWarning] = []
    for task in tasks:
        if classify_task_time_impact(task) != "warning":
            continue
        warnings.append(
            ScheduleWarning(
                source_id=task.page_id,
                source_name=task.name,
                source_type="Task",
                start=task.start,
                end=task.end,
            )
        )
    return warnings


def warnings_for_projects(projects: list[ProjectItem]) -> list[ScheduleWarning]:
    warnings: list[ScheduleWarning] = []
    for project in projects:
        if project.status in INACTIVE_PROJECT_STATUSES:
            continue
        if not project.start and not project.end:
            continue
        warnings.append(
            ScheduleWarning(
                source_id=project.page_id,
                source_name=project.name,
                source_type="Project",
                start=project.start,
                end=project.end,
            )
        )
    return warnings


def busy_intervals_for_course_sessions(sessions: list["CourseSession"]) -> list[BusyInterval]:
    intervals: list[BusyInterval] = []
    for session in sessions:
        if session.status in CANCELLED_SESSION_STATUSES:
            continue
        if not session.start or not session.end or session.end <= session.start:
            continue
        intervals.append(
            BusyInterval(
                source_id=session.page_id,
                source_name=session.name,
                source_type="CourseSession",
                start=session.start,
                end=session.end,
            )
        )
    return intervals


def split_availability_by_busy_intervals(
    start: datetime,
    end: datetime,
    busy_intervals: list[BusyInterval],
) -> list[tuple[datetime, datetime]]:
    windows = [(start, end)]
    for busy in sorted(busy_intervals, key=lambda item: item.start):
        next_windows: list[tuple[datetime, datetime]] = []
        for window_start, window_end in windows:
            if not _busy_overlaps_window(busy, window_start, window_end):
                next_windows.append((window_start, window_end))
                continue
            if window_start < busy.start:
                next_windows.append((window_start, min(busy.start, window_end)))
            if busy.end < window_end:
                next_windows.append((max(busy.end, window_start), window_end))
        windows = [
            (window_start, window_end)
            for window_start, window_end in next_windows
            if window_end > window_start
        ]
        if not windows:
            break
    return windows


def conflicts_for_window(
    start: datetime,
    end: datetime,
    busy_intervals: list[BusyInterval],
) -> list[ScheduleConflict]:
    return [
        ScheduleConflict(
            source_id=busy.source_id,
            source_name=busy.source_name,
            source_type=busy.source_type,
            start=max(start, busy.start),
            end=min(end, busy.end),
        )
        for busy in busy_intervals
        if _busy_overlaps_window(busy, start, end)
    ]


def warnings_for_window(
    start: datetime,
    end: datetime,
    warnings: list[ScheduleWarning],
) -> list[ScheduleWarning]:
    return [
        warning for warning in warnings
        if _warning_overlaps_window(warning, start, end)
    ]


async def get_projects_for_artist(artist_page_id: str) -> list[ProjectItem]:
    pages = await notion_api.query_data_source(
        config.DS_PROJECTS,
        filter_={"property": "關聯藝人", "relation": {"contains": artist_page_id}},
    )
    return [_to_project(page) for page in pages]


async def get_project_map() -> dict[str, ProjectItem]:
    pages = await notion_api.query_data_source(config.DS_PROJECTS)
    return {page["id"]: _to_project(page) for page in pages}


async def get_tasks_for_artist(artist_page_id: str) -> list[TaskItem]:
    project_map = await get_project_map()
    pages = await notion_api.query_data_source(config.DS_TASKS)
    tasks = [_to_task(page) for page in pages]
    return [
        task for task in tasks
        if task_applies_to_artist(task, artist_page_id, project_map)
    ]


# ---------- P0-2 任務查詢（Discord 顯示） ----------

TASK_DUE_SOON_DAYS = 3


async def get_discord_tasks_for_artist(artist_page_id: str) -> list[TaskItem]:
    """/我的任務：Discord 顯示＋直接關聯該藝人（Notion 端過濾，不撈全表）。"""
    filter_ = {"and": [
        {"property": TASK_DISCORD_DISPLAY_PROPERTY, "checkbox": {"equals": True}},
        {"property": "關聯藝人", "relation": {"contains": artist_page_id}},
    ]}
    try:
        pages = await notion_api.query_data_source(config.DS_TASKS, filter_=filter_)
    except NotionAPIError as e:
        if TASK_DISCORD_DISPLAY_PROPERTY not in str(e):
            raise
        legacy_filter = {"and": [
            {"property": TASK_DISCORD_VISIBLE_LEGACY_PROPERTY, "checkbox": {"equals": True}},
            {"property": "關聯藝人", "relation": {"contains": artist_page_id}},
        ]}
        pages = await notion_api.query_data_source(config.DS_TASKS, filter_=legacy_filter)
    return [_to_task(p) for p in pages]


async def get_active_tasks() -> list[TaskItem]:
    """管理端 /任務總覽：所有未結案任務（含狀態未填的搬移舊資料）。"""
    conditions: list[dict] = [
        {"property": "任務狀態", "select": {"equals": s}} for s in sorted(ACTIVE_TASK_STATUSES)
    ]
    conditions.append({"property": "任務狀態", "select": {"is_empty": True}})
    pages = await notion_api.query_data_source(config.DS_TASKS, filter_={"or": conditions})
    return [_to_task(p) for p in pages]


def task_deadline_passed(task: TaskItem, now: datetime) -> bool:
    """截止日是否已過。date-only 截止視為當天整天有效，隔天才算逾期。"""
    if not task.end:
        return False
    if task.end_has_time:
        return task.end < now
    return now.date() > task.end.date()


def task_due_within(task: TaskItem, now: datetime, days: int = TASK_DUE_SOON_DAYS) -> bool:
    if not task.end or task_deadline_passed(task, now):
        return False
    return (task.end.date() - now.date()).days <= days


def classify_tasks_for_display(tasks: list[TaskItem], now: datetime) -> dict[str, list[TaskItem]]:
    """把任務分到互斥顯示區，各區依截止日排序（無截止日排最後）。

    分區優先序：待審核（已交出、球在公司）→ 逾期 → 需修改 → 即將到期 → 未完成。
    已完成/已取消/已封存不顯示。
    """
    buckets: dict[str, list[TaskItem]] = {
        "overdue": [], "needs_fix": [], "due_soon": [], "in_review": [], "open": [],
    }
    for task in tasks:
        if task.status in INACTIVE_TASK_STATUSES:
            continue
        if task.status == "待審核":
            buckets["in_review"].append(task)
        elif task.status == "已逾期" or task_deadline_passed(task, now):
            buckets["overdue"].append(task)
        elif task.status == "需修改":
            buckets["needs_fix"].append(task)
        elif task_due_within(task, now):
            buckets["due_soon"].append(task)
        else:
            buckets["open"].append(task)

    def _sort_key(t: TaskItem):
        return (t.end is None, t.end or now)

    for items in buckets.values():
        items.sort(key=_sort_key)
    return buckets


# ---------- P0-4 任務提醒 ----------

# Tasks.提醒規則 multi-select 的合法值 → 對應「截止日前幾天」
REMINDER_RULE_DAYS = {"3 天前": 3, "1 天前": 1, "當天": 0}
OVERDUE_STAGE = "逾期"


def compute_due_reminder_stages(task: TaskItem, now: datetime) -> list[str]:
    """回傳此刻該觸發的提醒階段（去重由呼叫端的 _once 處理）。

    - 「3 天前 / 1 天前 / 當天」完全依任務的提醒規則設定，且用 <= 判斷：
      loop 每 30 分鐘跑一次，若 bot 停機錯過整點，補跑時仍會觸發一次。
    - 「逾期」不看規則、一律觸發（Kickoff：逾期需提醒或升級處理），
      公司沒勾規則的任務也不會默默爛掉。
    """
    if task.status in INACTIVE_TASK_STATUSES or not task.end:
        return []
    if task_deadline_passed(task, now):
        return [OVERDUE_STAGE]
    days_left = (task.end.date() - now.date()).days
    return [
        rule for rule, days in REMINDER_RULE_DAYS.items()
        if rule in task.reminder_rules and days_left <= days
    ]


async def update_task_status(page_id: str, status: str) -> None:
    await notion_api.update_page(page_id, {"任務狀態": _select(status)})


# 「撞課」情境（Tasks/Projects/Sessions）每次填時段都要算，且含兩次全表掃描。
# 以 artist page id 為 key 做短 TTL 快取（Notion page id 全域唯一，跨租戶不會撞），
# 讓同一藝人連續填多筆、或一次批次多筆時只查一次 Notion。
_IMPACT_CONTEXT_TTL = 30.0  # 秒
_impact_context_cache: dict[str, tuple[float, "ScheduleImpactContext"]] = {}


def _invalidate_impact_context_cache(artist_page_id: str | None = None) -> None:
    """清掉撞課情境快取（None＝全清）。正式安排有變動時可呼叫，讓下次填寫看到最新。"""
    if artist_page_id is None:
        _impact_context_cache.clear()
    else:
        _impact_context_cache.pop(artist_page_id, None)


async def get_schedule_impact_context_for_artist(
    artist_page_id: str, use_cache: bool = True
) -> ScheduleImpactContext:
    now = time.monotonic()
    if use_cache:
        cached = _impact_context_cache.get(artist_page_id)
        if cached is not None and now - cached[0] < _IMPACT_CONTEXT_TTL:
            return cached[1]
    tasks = await get_tasks_for_artist(artist_page_id)
    projects = await get_projects_for_artist(artist_page_id)
    sessions = await get_course_sessions_for_artist(artist_page_id)
    context = ScheduleImpactContext(
        busy_intervals=busy_intervals_for_tasks(tasks) + busy_intervals_for_course_sessions(sessions),
        warnings=warnings_for_tasks(tasks) + warnings_for_projects(projects),
    )
    _impact_context_cache[artist_page_id] = (now, context)
    return context


# ---------- Availability Slots ----------

@dataclass
class AvailabilitySlot:
    page_id: str
    name: str
    request_page_ids: list[str]
    artist_page_ids: list[str]
    start: datetime | None
    end: datetime | None
    note: str = ""


@dataclass
class MonthlyPoolMatchResult:
    participants: dict[str, list[overlap.Interval]]
    windows: list[overlap.Interval]
    missing_artist_page_ids: list[str]


@dataclass
class MonthlyPoolProgress:
    filled_artist_page_ids: list[str]
    missing_artist_page_ids: list[str]

    @property
    def is_complete(self) -> bool:
        return not self.missing_artist_page_ids


@dataclass
class AvailabilitySlotCompaction:
    visible: list[AvailabilitySlot]
    redundant: list[AvailabilitySlot]
    updates: list["AvailabilitySlotWindowUpdate"] = field(default_factory=list)


@dataclass(frozen=True)
class AvailabilitySlotWindowUpdate:
    page_id: str
    start: datetime
    end: datetime


def _to_slot(page: dict) -> AvailabilitySlot:
    return AvailabilitySlot(
        page_id=page["id"],
        name=_read_title(page, "可用時段名稱"),
        request_page_ids=_read_relation_ids(page, "關聯排課需求"),
        artist_page_ids=_read_relation_ids(page, "填寫藝人"),
        start=_read_date_start(page, "可用開始時間"),
        end=_read_date_start(page, "可用結束時間"),
        note=_read_rich_text(page, "備註"),
    )


def _slot_scope(slot: AvailabilitySlot) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return (
        tuple(sorted(slot.artist_page_ids)),
        tuple(sorted(slot.request_page_ids)),
    )


def _note_field(note: str, field_name: str) -> str | None:
    prefix = f"{field_name}："
    for part in note.split("｜"):
        part = part.strip()
        if part.startswith(prefix):
            return part[len(prefix):].strip() or None
    return None


def _slot_nature_priority(slot: AvailabilitySlot) -> int:
    return 1


def _slot_nature(slot: AvailabilitySlot) -> str:
    nature = time_collection.normalize_collection_nature_label(
        _note_field(slot.note, "收集性質")
    )
    return nature or ""


def _slot_compaction_sort_key(slot: AvailabilitySlot) -> tuple:
    start = slot.start or datetime.max.replace(tzinfo=config.TZ)
    end = slot.end or datetime.min.replace(tzinfo=config.TZ)
    end_seconds = end.toordinal() * 86400 + end.hour * 3600 + end.minute * 60 + end.second
    return (_slot_scope(slot), start, -end_seconds, -_slot_nature_priority(slot), slot.page_id)


def _slot_merge_key(slot: AvailabilitySlot) -> tuple | None:
    if not slot.start or not slot.end:
        return None
    return (_slot_scope(slot), _slot_nature(slot), slot.start.date())


def _slots_overlap(left: AvailabilitySlot, right: AvailabilitySlot) -> bool:
    if not left.start or not left.end or not right.start or not right.end:
        return False
    return left.start <= right.end and right.start <= left.end


def _can_replace_slot(container: AvailabilitySlot, contained: AvailabilitySlot) -> bool:
    if not container.start or not container.end or not contained.start or not contained.end:
        return False
    if _slot_scope(container) != _slot_scope(contained):
        return False
    if container.start > contained.start or container.end < contained.end:
        return False
    if container.start == contained.start and container.end == contained.end:
        return True
    return _slot_nature_priority(container) >= _slot_nature_priority(contained)


def compact_availability_slots(slots: list[AvailabilitySlot]) -> AvailabilitySlotCompaction:
    """Remove exact duplicates and redundant contained windows from a slot list."""
    visible: list[AvailabilitySlot] = []
    redundant: list[AvailabilitySlot] = []
    for slot in sorted(slots, key=_slot_compaction_sort_key):
        if not slot.start or not slot.end:
            visible.append(slot)
            continue
        replaced_existing: list[AvailabilitySlot] = []
        is_redundant = False
        for kept in visible:
            if _can_replace_slot(kept, slot):
                redundant.append(slot)
                is_redundant = True
                break
            if _can_replace_slot(slot, kept):
                replaced_existing.append(kept)
        if is_redundant:
            continue
        for replaced in replaced_existing:
            visible.remove(replaced)
            redundant.append(replaced)
        visible.append(slot)
    visible, merged_redundant, updates = _merge_overlapping_same_nature_slots(visible)
    redundant.extend(merged_redundant)
    return AvailabilitySlotCompaction(
        visible=sorted(visible, key=_slot_compaction_sort_key),
        redundant=sorted(redundant, key=_slot_compaction_sort_key),
        updates=updates,
    )


def _merge_overlapping_same_nature_slots(
    slots: list[AvailabilitySlot],
) -> tuple[list[AvailabilitySlot], list[AvailabilitySlot], list[AvailabilitySlotWindowUpdate]]:
    sorted_slots = sorted(slots, key=_slot_compaction_sort_key)
    visible: list[AvailabilitySlot] = []
    redundant: list[AvailabilitySlot] = []
    updates: list[AvailabilitySlotWindowUpdate] = []
    while sorted_slots:
        base = sorted_slots.pop(0)
        merge_key = _slot_merge_key(base)
        if merge_key is None:
            visible.append(base)
            continue
        group = [base]
        changed = True
        while changed:
            changed = False
            merged_start = min(slot.start for slot in group if slot.start)
            merged_end = max(slot.end for slot in group if slot.end)
            remaining: list[AvailabilitySlot] = []
            merged_window = replace(base, start=merged_start, end=merged_end)
            for candidate in sorted_slots:
                if _slot_merge_key(candidate) == merge_key and _slots_overlap(merged_window, candidate):
                    group.append(candidate)
                    changed = True
                else:
                    remaining.append(candidate)
            sorted_slots = remaining
        keeper = sorted(group, key=_slot_compaction_sort_key)[0]
        merged_start = min(slot.start for slot in group if slot.start)
        merged_end = max(slot.end for slot in group if slot.end)
        merged_keeper = replace(keeper, start=merged_start, end=merged_end)
        visible.append(merged_keeper)
        redundant.extend(slot for slot in group if slot.page_id != keeper.page_id)
        if keeper.start != merged_start or keeper.end != merged_end:
            updates.append(AvailabilitySlotWindowUpdate(keeper.page_id, merged_start, merged_end))
    return visible, redundant, updates


def find_duplicate_availability_slot(
    slots: list[AvailabilitySlot],
    request_page_id: str | None,
    artist_page_id: str,
    start: datetime,
    end: datetime,
) -> AvailabilitySlot | None:
    """Find an existing active slot for the same artist, scope, and exact time window."""
    for slot in slots:
        if artist_page_id not in slot.artist_page_ids:
            continue
        if request_page_id is None:
            if slot.request_page_ids:
                continue
        elif request_page_id not in slot.request_page_ids:
            continue
        if slot.start == start and slot.end == end:
            return slot
    return None


def _build_availability_slot_props(
    request_page_id: str | None,
    artist: Artist,
    start: datetime,
    end: datetime,
    is_late: bool,
    note: str = "",
) -> dict:
    name = f"{artist.name}｜{start.strftime('%m/%d %H:%M')}-{end.strftime('%H:%M')}"
    props = {
        "可用時段名稱": _title(name),
        "填寫藝人": _relation([artist.page_id]),
        "填寫人名稱": _rich_text(artist.name),
        "身份": _select("藝人"),
        "來源": _select("Discord"),
        "可用開始時間": _date(start),
        "可用結束時間": _date(end),
        "提交時間": _date(datetime.now(config.TZ)),
        "是否逾期提交": _checkbox(is_late),
    }
    if request_page_id:
        props["關聯排課需求"] = _relation([request_page_id])
    if note:
        props["備註"] = _rich_text(note)
    return props


async def create_availability_slot(
    request_page_id: str | None,
    artist: Artist,
    start: datetime,
    end: datetime,
    is_late: bool,
    note: str = "",
) -> AvailabilitySlot:
    props = _build_availability_slot_props(request_page_id, artist, start, end, is_late, note)
    page = await notion_api.create_page(config.DS_AVAILABILITY_SLOTS, props)
    return _to_slot(page)


async def create_availability_slot_if_absent(
    request_page_id: str | None,
    artist: Artist,
    start: datetime,
    end: datetime,
    is_late: bool,
    note: str = "",
    existing_slots: list[AvailabilitySlot] | None = None,
) -> tuple[AvailabilitySlot, bool]:
    """Create a slot unless an identical active slot already exists.

    Returns (slot, created). When created is False, slot is the existing duplicate.
    """
    slots = existing_slots if existing_slots is not None else await get_slots_for_artist(artist.page_id)
    duplicate = find_duplicate_availability_slot(
        slots,
        request_page_id=request_page_id,
        artist_page_id=artist.page_id,
        start=start,
        end=end,
    )
    if duplicate is not None:
        return duplicate, False
    created = await create_availability_slot(request_page_id, artist, start, end, is_late, note)
    slots.append(created)
    return created, True


def availability_slot_month(slot: AvailabilitySlot) -> str | None:
    """Return the collection month for a pool slot.

    New monthly collection slots write `收集月份：YYYY-MM` into notes. Legacy slots
    do not have that marker, so they fall back to the slot start month.
    """
    explicit_month = _note_field(slot.note, "收集月份")
    if explicit_month:
        return time_collection.normalize_month(explicit_month)
    if slot.start:
        return slot.start.strftime("%Y-%m")
    return None


def is_monthly_pool_slot(slot: AvailabilitySlot, month: str) -> bool:
    normalized_month = time_collection.normalize_month(month)
    if normalized_month is None:
        return False
    if slot.request_page_ids:
        return False
    if not slot.start or not slot.end or slot.end <= slot.start:
        return False
    return availability_slot_month(slot) == normalized_month


def candidate_start_times(
    start: datetime,
    end: datetime,
    duration_minutes: int,
    step_minutes: int = 30,
) -> list[datetime]:
    """Return valid start times inside a candidate window, stepping by 30 minutes."""
    if duration_minutes <= 0 or step_minutes <= 0 or end <= start:
        return []
    latest_start = end - timedelta(minutes=duration_minutes)
    starts: list[datetime] = []
    current = start
    while current <= latest_start:
        starts.append(current)
        current += timedelta(minutes=step_minutes)
    return starts


def format_duration_hours(duration_minutes: int) -> str:
    hours = duration_minutes / 60
    if hours.is_integer():
        return f"{int(hours)} 小時"
    text = f"{hours:.2f}".rstrip("0").rstrip(".")
    return f"{text} 小時"


def match_monthly_pool_windows(
    *,
    slots: list[AvailabilitySlot],
    artist_page_ids: list[str],
    month: str,
    duration_minutes: int,
    busy_by_artist_page_id: dict[str, list[BusyInterval]] | None = None,
) -> MonthlyPoolMatchResult:
    """Build common availability windows from monthly pool slots.

    The pool only includes slots not tied to a Schedule Request. Each selected
    artist's slots are split by formal busy intervals before the all-person
    intersection is calculated.
    """
    normalized_month = time_collection.normalize_month(month)
    if normalized_month is None:
        raise ValueError(f"Invalid month: {month}")

    busy_by_artist_page_id = busy_by_artist_page_id or {}
    participants: dict[str, list[overlap.Interval]] = {
        artist_page_id: [] for artist_page_id in artist_page_ids
    }
    for slot in slots:
        if not is_monthly_pool_slot(slot, normalized_month):
            continue
        for artist_page_id in slot.artist_page_ids:
            if artist_page_id not in participants:
                continue
            split_windows = split_availability_by_busy_intervals(
                slot.start,
                slot.end,
                busy_by_artist_page_id.get(artist_page_id, []),
            )
            participants[artist_page_id].extend(split_windows)

    participants = {
        artist_page_id: overlap.merge_intervals(intervals)
        for artist_page_id, intervals in participants.items()
    }
    missing_artist_page_ids = [
        artist_page_id for artist_page_id, intervals in participants.items()
        if not intervals
    ]
    windows = (
        [] if missing_artist_page_ids
        else overlap.find_common_windows(participants, duration_minutes)
    )
    return MonthlyPoolMatchResult(
        participants=participants,
        windows=windows,
        missing_artist_page_ids=missing_artist_page_ids,
    )


def monthly_pool_progress_from_slots(
    slots: list[AvailabilitySlot],
    artist_page_ids: list[str],
    month: str,
) -> MonthlyPoolProgress:
    expected_ids = list(dict.fromkeys(artist_page_ids))
    filled_ids: set[str] = set()
    for slot in slots:
        if not is_monthly_pool_slot(slot, month):
            continue
        for artist_page_id in slot.artist_page_ids:
            if artist_page_id in expected_ids:
                filled_ids.add(artist_page_id)
    filled = [artist_page_id for artist_page_id in expected_ids if artist_page_id in filled_ids]
    missing = [artist_page_id for artist_page_id in expected_ids if artist_page_id not in filled_ids]
    return MonthlyPoolProgress(filled_artist_page_ids=filled, missing_artist_page_ids=missing)


async def get_monthly_pool_progress(
    month: str,
    artist_page_ids: list[str],
) -> MonthlyPoolProgress:
    slots = await get_monthly_pool_slots_for_artists(artist_page_ids)
    return monthly_pool_progress_from_slots(slots, artist_page_ids, month)


async def get_slots_for_request(request_page_id: str) -> list[AvailabilitySlot]:
    pages = await notion_api.query_data_source(
        config.DS_AVAILABILITY_SLOTS,
        filter_={"property": "關聯排課需求", "relation": {"contains": request_page_id}},
    )
    return [_to_slot(p) for p in pages]


def _future_date_filter(property_name: str) -> dict:
    """Notion 端粗篩：只撈「日期 >= 今天」的頁面（天級門檻）。

    精細的「end > now」過濾仍在 Python 端做；這層只是避免把幾個月的
    歷史時段整批撈回來（時間池會隨月份無限累積，全撈會越來越慢）。
    注意：該日期欄位為空的頁面會被 Notion 過濾掉——現有呼叫端本來就
    只處理有結束時間的時段，語意不變。
    """
    today = datetime.now(config.TZ).date().isoformat()
    return {"property": property_name, "date": {"on_or_after": today}}


async def get_slots_for_artist(
    artist_page_id: str, future_only: bool = True
) -> list[AvailabilitySlot]:
    filter_: dict = {"property": "填寫藝人", "relation": {"contains": artist_page_id}}
    if future_only:
        filter_ = {"and": [filter_, _future_date_filter("可用結束時間")]}
    pages = await notion_api.query_data_source(config.DS_AVAILABILITY_SLOTS, filter_=filter_)
    return [_to_slot(p) for p in pages]


async def get_monthly_pool_slots_for_artists(
    artist_page_ids: list[str],
    future_only: bool = True,
) -> list[AvailabilitySlot]:
    """Fetch pool candidate slots for selected artists, deduping shared pages."""
    slots_by_id: dict[str, AvailabilitySlot] = {}
    for artist_page_id in artist_page_ids:
        for slot in await get_slots_for_artist(artist_page_id, future_only=future_only):
            slots_by_id[slot.page_id] = slot
    return list(slots_by_id.values())


async def get_all_availability_slots(future_only: bool = True) -> list[AvailabilitySlot]:
    filter_ = _future_date_filter("可用結束時間") if future_only else None
    pages = await notion_api.query_data_source(config.DS_AVAILABILITY_SLOTS, filter_=filter_)
    return [_to_slot(p) for p in pages]


async def archive_slot(page_id: str) -> None:
    try:
        await notion_api.update_page(page_id, archived=True)
    except notion_api.NotionAPIError as e:
        if e.status_code == 400 and "already archived" in str(e):
            return
        if e.status_code == 400 and "is archived" in str(e):
            return
        raise


async def update_availability_slot_window(page_id: str, start: datetime, end: datetime) -> None:
    await notion_api.update_page(page_id, {
        "可用開始時間": _date(start),
        "可用結束時間": _date(end),
    })


async def apply_availability_slot_compaction(
    slots: list[AvailabilitySlot],
) -> AvailabilitySlotCompaction:
    compaction = compact_availability_slots(slots)
    for update in compaction.updates:
        await update_availability_slot_window(update.page_id, update.start, update.end)
    for slot in compaction.redundant:
        await archive_slot(slot.page_id)
    return compaction


# ---------- Course Sessions ----------

@dataclass
class CourseSession:
    page_id: str
    name: str
    status: str | None
    artist_page_ids: list[str] = field(default_factory=list)
    start: datetime | None = None
    end: datetime | None = None


def _to_course_session(page: dict) -> CourseSession:
    return CourseSession(
        page_id=page["id"],
        name=_read_title(page, "課程名稱"),
        status=_read_select(page, "課程狀態"),
        artist_page_ids=_read_relation_ids(page, "參與藝人"),
        start=_read_date_start(page, "上課時間"),
        end=_read_date_end(page, "上課時間"),
    )


CANCELLED_SESSION_STATUSES = {"已取消", "取消", "取消 / 改期", "已改期"}


def occupied_session_dates_for_artist(
    sessions: list[CourseSession],
    artist_page_id: str,
    now: datetime | None = None,
) -> set:
    now = now or datetime.now(config.TZ)
    occupied_dates = set()
    for session in sessions:
        if artist_page_id not in session.artist_page_ids:
            continue
        if session.status in CANCELLED_SESSION_STATUSES:
            continue
        if not session.start:
            continue
        session_end = session.end or session.start
        if session_end <= now:
            continue
        occupied_dates.add(session.start.date())
    return occupied_dates


async def get_course_sessions_for_artist(
    artist_page_id: str, future_only: bool = True
) -> list[CourseSession]:
    filter_: dict = {"property": "參與藝人", "relation": {"contains": artist_page_id}}
    if future_only:
        # 撞課檢查只對未來的填寫有意義，過去的課程不需要撈回來
        filter_ = {"and": [filter_, _future_date_filter("上課時間")]}
    pages = await notion_api.query_data_source(config.DS_COURSE_SESSIONS, filter_=filter_)
    return [_to_course_session(page) for page in pages]


async def get_occupied_session_dates_for_artist(artist_page_id: str) -> set:
    sessions = await get_course_sessions_for_artist(artist_page_id)
    return occupied_session_dates_for_artist(sessions, artist_page_id)


async def create_course_session(
    name: str,
    course_type: str,
    artist_page_ids: list[str],
    teacher: str,
    start: datetime,
    end: datetime,
    request_page_id: str,
    location: str = "",
) -> dict:
    props = {
        "課程名稱": _title(name),
        "課程類型": _select(course_type),
        "課程狀態": _select("已排定"),
        "參與藝人": _relation(artist_page_ids),
        "老師": _rich_text(teacher),
        "上課時間": _date(start, end),
        "關聯排課需求": _relation([request_page_id]),
    }
    if location:
        props["地點"] = _rich_text(location)
    return await notion_api.create_page(config.DS_COURSE_SESSIONS, props)


async def mark_session_notified(page_id: str, notify_record: str) -> None:
    await notion_api.update_page(page_id, {
        "課程狀態": _select("已通知"),
        "通告發布時間": _date(datetime.now(config.TZ)),
        "Discord 通知紀錄": _rich_text(notify_record),
    })
