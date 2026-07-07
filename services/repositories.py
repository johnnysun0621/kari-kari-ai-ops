"""五張 P0 資料庫的 CRUD。欄位名稱 → Notion API payload 的唯一對應處。

Notion 欄位名稱是中文（與正式資料庫完全一致），若 Notion 端改名，只需改這裡。
"""
from dataclasses import dataclass, field
from datetime import datetime

import config
from services import notion_api

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


def _read_relation_ids(page: dict, name: str) -> list[str]:
    return [r["id"] for r in page["properties"].get(name, {}).get("relation", [])]


def _read_unique_id(page: dict, name: str) -> int | None:
    uid = page["properties"].get(name, {}).get("unique_id")
    return uid.get("number") if uid else None


# ---------- Artists ----------

@dataclass
class Artist:
    page_id: str
    name: str
    discord_user_id: str
    status: str | None = None


def _to_artist(page: dict) -> Artist:
    return Artist(
        page_id=page["id"],
        name=_read_title(page, "藝人名稱"),
        discord_user_id=_read_rich_text(page, "Discord User ID").strip(),
        status=_read_select(page, "藝人狀態"),
    )


async def get_active_artists() -> list[Artist]:
    pages = await notion_api.query_data_source(
        config.DS_ARTISTS,
        filter_={"property": "藝人狀態", "select": {"equals": "Active"}},
    )
    return [a for a in map(_to_artist, pages) if a.discord_user_id]


async def get_artists_by_ids(page_ids: list[str]) -> list[Artist]:
    """依 relation page id 取藝人（撈 Active 全表後過濾，P0 資料量小）。"""
    pages = await notion_api.query_data_source(config.DS_ARTISTS)
    wanted = set(page_ids)
    return [a for a in map(_to_artist, pages) if a.page_id in wanted]


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


OPEN_REQUEST_STATUSES = ["等待藝人填寫", "比對中", "等待老師確認", "已確認"]


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
    pages = await notion_api.query_data_source(config.DS_SCHEDULE_REQUESTS)
    for p in pages:
        if p["id"] == page_id:
            return _to_request(p)
    return None


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


async def create_availability_slot(
    request_page_id: str,
    artist: Artist,
    start: datetime,
    end: datetime,
    is_late: bool,
    note: str = "",
) -> AvailabilitySlot:
    name = f"{artist.name}｜{start.strftime('%m/%d %H:%M')}-{end.strftime('%H:%M')}"
    props = {
        "可用時段名稱": _title(name),
        "關聯排課需求": _relation([request_page_id]),
        "填寫藝人": _relation([artist.page_id]),
        "填寫人名稱": _rich_text(artist.name),
        "身份": _select("藝人"),
        "來源": _select("Discord"),
        "可用開始時間": _date(start),
        "可用結束時間": _date(end),
        "提交時間": _date(datetime.now(config.TZ)),
        "是否逾期提交": _checkbox(is_late),
    }
    if note:
        props["備註"] = _rich_text(note)
    page = await notion_api.create_page(config.DS_AVAILABILITY_SLOTS, props)
    return _to_slot(page)


async def get_slots_for_request(request_page_id: str) -> list[AvailabilitySlot]:
    pages = await notion_api.query_data_source(
        config.DS_AVAILABILITY_SLOTS,
        filter_={"property": "關聯排課需求", "relation": {"contains": request_page_id}},
    )
    return [_to_slot(p) for p in pages]


async def archive_slot(page_id: str) -> None:
    await notion_api.update_page(page_id, archived=True)


# ---------- Course Sessions ----------

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
