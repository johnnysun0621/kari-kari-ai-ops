"""五張 P0 資料庫的 CRUD。欄位名稱 → Notion API payload 的唯一對應處。

Notion 欄位名稱是中文（與正式資料庫完全一致），若 Notion 端改名，只需改這裡。
"""
import logging
import time
from io import BytesIO
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

import httpx
from docx import Document
from openpyxl import load_workbook
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from pptx import Presentation

import config
from services import notion_api, overlap, time_collection
from services.notion_api import NotionAPIError

logger = logging.getLogger(__name__)

_MAX_FILE_BYTES = 20 * 1024 * 1024
_MAX_FILE_TEXT_CHARS = 12000

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


def _read_people_names(page: dict, name: str) -> list[str]:
    """讀取 Notion person 欄位的顯示名稱；未授權讀取時保留空值。"""
    people = page.get("properties", {}).get(name, {}).get("people", [])
    names: list[str] = []
    for person in people:
        name_text = person.get("name") or person.get("person", {}).get("email") or ""
        if name_text:
            names.append(name_text)
    return names


def _read_unique_id(page: dict, name: str) -> int | None:
    uid = page["properties"].get(name, {}).get("unique_id")
    return uid.get("number") if uid else None


# ---------- Notion Bulletin ----------

@dataclass(frozen=True)
class BulletinItem:
    page_id: str
    url: str
    title: str
    category: str
    status: str
    date: datetime | None
    people: tuple[str, ...] = ()
    pinned: bool = False


def _to_bulletin(page: dict) -> BulletinItem:
    return BulletinItem(
        page_id=page["id"],
        url=page.get("url", ""),
        title=_read_title(page, "Name") or "未命名事項",
        category=_read_select(page, "類別") or "未分類",
        status=_read_select(page, "狀態") or "未處理",
        date=_read_date_start(page, "Date"),
        people=tuple(_read_people_names(page, "人員")),
        pinned=_read_first_checkbox(page, ["📌 置頂", "置頂"]),
    )


async def get_bulletin_items() -> list[BulletinItem]:
    """讀取 KariKari Testing 布告欄；排序交給 Discord 顯示層處理。"""
    pages = await notion_api.query_data_source(
        config.DS_BULLETIN,
        sorts=[{"property": "Date", "direction": "descending"}],
    )
    return [_to_bulletin(page) for page in pages if not page.get("archived")]


@dataclass(frozen=True)
class CourseAttachment:
    name: str
    extension: str
    text: str


@dataclass(frozen=True)
class CourseMeetingRecord:
    page_id: str
    url: str
    title: str
    date: datetime | None
    summary: str
    content: str
    artist_page_ids: tuple[str, ...]
    task_page_ids: tuple[str, ...]
    project_page_ids: tuple[str, ...]
    attachments: tuple[CourseAttachment, ...] = ()


def _block_plain_text(block: dict) -> str:
    data = block.get(block.get("type", ""), {})
    rich_text = data.get("rich_text", []) if isinstance(data, dict) else []
    return "".join(item.get("plain_text", "") for item in rich_text).strip()


def _file_block_url(block: dict) -> str:
    """取得 Notion file block 的 signed URL 或 external URL。"""
    if block.get("type") != "file":
        return ""
    candidates = []
    for root in (block.get("file"), block.get("external")):
        if isinstance(root, dict):
            candidates.extend([root, root.get("file"), root.get("external")])
    for data in candidates:
        if not isinstance(data, dict):
            continue
        source = data.get("url")
        if not source and isinstance(data.get("external"), dict):
            source = data["external"].get("url")
        if isinstance(source, str) and source:
            return source
    return ""


def _file_block_name(block: dict, url: str) -> str:
    candidates = [block.get("file"), block.get("external")]
    for data in candidates:
        if isinstance(data, dict) and isinstance(data.get("name"), str) and data["name"]:
            return data["name"]
    return urlparse(url).path.rsplit("/", 1)[-1]


def _file_extension(block: dict, url: str, content_type: str = "") -> str:
    name = _file_block_name(block, url).lower().split("?", 1)[0]
    if "." in name:
        return "." + name.rsplit(".", 1)[-1]
    return {
        "application/pdf": ".pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    }.get(content_type.split(";", 1)[0].lower(), "")


def _extract_file_text(data: bytes, extension: str) -> str:
    if extension == ".pdf":
        reader = PdfReader(BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if extension == ".docx":
        document = Document(BytesIO(data))
        paragraphs = [paragraph.text for paragraph in document.paragraphs]
        paragraphs.extend(cell.text for table in document.tables for row in table.rows for cell in row.cells)
        return "\n".join(text for text in paragraphs if text.strip())
    if extension == ".pptx":
        presentation = Presentation(BytesIO(data))
        return "\n".join(
            shape.text
            for slide in presentation.slides
            for shape in slide.shapes
            if hasattr(shape, "text") and shape.text.strip()
        )
    if extension == ".xlsx":
        workbook = load_workbook(BytesIO(data), read_only=True, data_only=True)
        lines = []
        for sheet in workbook.worksheets:
            lines.append(f"【工作表：{sheet.title}】")
            for row in sheet.iter_rows(values_only=True):
                values = [str(value) for value in row if value is not None]
                if values:
                    lines.append("｜".join(values))
        return "\n".join(lines)
    if extension in {".txt", ".md", ".csv", ".json", ".yaml", ".yml"}:
        return data.decode("utf-8", errors="replace")
    return ""


async def _file_block_text(block: dict) -> str:
    """下載 Notion file block 並抽取常見文件文字；失敗時略過其他內容。"""
    url = _file_block_url(block)
    if not url:
        return ""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url)
            response.raise_for_status()
        if len(response.content) > _MAX_FILE_BYTES:
            logger.warning("課程檔案超過可解析大小上限：%d bytes", len(response.content))
            return ""
        extension = _file_extension(block, url, response.headers.get("content-type", ""))
        text = _extract_file_text(response.content, extension)
        if not text and extension not in {".pdf", ".docx", ".pptx", ".xlsx"}:
            logger.info("略過不支援的課程檔案格式：%s", extension or "unknown")
        return text.strip()[:_MAX_FILE_TEXT_CHARS]
    except (httpx.HTTPError, OSError, PdfReadError, ValueError, TypeError, KeyError) as exc:
        logger.warning("讀取課程檔案失敗：%s", type(exc).__name__)
        return ""


async def _page_content(
    page_id: str,
    *,
    _visited: set[str] | None = None,
    _attachments: list[CourseAttachment] | None = None,
) -> str:
    """讀取頁面正文、巢狀區塊、Synced Block 與 PDF file block。"""
    visited = _visited or set()
    if page_id in visited:
        return ""
    visited.add(page_id)
    try:
        blocks = await notion_api.retrieve_block_children(page_id)
    except notion_api.NotionAPIError as exc:
        # 同步來源頁未 share 給 integration 時，主紀錄仍可用摘要回答。
        logger.warning("無法讀取課程正文（page=%s, status=%s）", page_id, exc.status_code)
        return ""
    lines: list[str] = []
    for block in blocks:
        text = _block_plain_text(block)
        if text:
            lines.append(text)
        block_type = block.get("type")
        data = block.get(block_type, {}) if block_type else {}
        if block_type == "file":
            file_url = _file_block_url(block)
            file_name = _file_block_name(block, file_url) or "未命名附件"
            extension = _file_extension(block, file_url)
            file_text = await _file_block_text(block)
            if _attachments is not None:
                _attachments.append(CourseAttachment(file_name, extension, file_text))
        elif block_type == "synced_block":
            source = data.get("synced_from") if isinstance(data, dict) else None
            child_id = source.get("block_id") if isinstance(source, dict) else None
            if child_id:
                nested = await _page_content(child_id, _visited=visited, _attachments=_attachments)
                if nested:
                    lines.append(nested)
        elif block.get("has_children"):
            nested = await _page_content(block["id"], _visited=visited, _attachments=_attachments)
            if nested:
                lines.append(nested)
    return "\n".join(lines)


async def get_course_meetings_for_artist(artist_page_id: str) -> list[CourseMeetingRecord]:
    """只回傳已勾選藝人可見，且與該藝人有直接或間接關聯的課程紀錄。"""
    pages = await notion_api.query_data_source(
        config.DS_COURSE_MEETINGS,
        filter_={"and": [
            {"property": "藝人可見", "checkbox": {"equals": True}},
            {"or": [
                {"property": "會議類型", "select": {"equals": "課程"}},
                {"property": "會議類型", "select": {"equals": "課程會議"}},
            ]},
        ]},
        sorts=[{"property": "Date", "direction": "descending"}],
    )
    project_map = await get_project_map()
    task_pages = await notion_api.query_data_source(
        config.DS_TASKS,
        filter_={"property": "課程與會議記錄", "relation": {"is_not_empty": True}},
    )
    tasks_by_meeting: dict[str, list[TaskItem]] = {}
    for task_page in task_pages:
        task = _to_task(task_page)
        for meeting_page_id in task.course_meeting_page_ids:
            tasks_by_meeting.setdefault(meeting_page_id, []).append(task)
    records: list[CourseMeetingRecord] = []
    for page in pages:
        artists = tuple(_read_relation_ids(page, "參與藝人"))
        related_tasks = tasks_by_meeting.get(page["id"], [])
        task_ids = tuple(task.page_id for task in related_tasks)
        project_ids = tuple(_read_relation_ids(page, "關聯專案"))
        related = artist_page_id in artists
        related = related or any(
            task_applies_to_artist(task, artist_page_id, project_map)
            for task in related_tasks
        )
        related = related or any(
            project_id in project_map and artist_page_id in project_map[project_id].artist_page_ids
            for project_id in project_ids
        )
        if not related:
            continue
        attachments: list[CourseAttachment] = []
        content = await _page_content(page["id"], _attachments=attachments)
        records.append(CourseMeetingRecord(
            page_id=page["id"],
            url=page.get("url", ""),
            title=_read_title(page, "Name"),
            date=_read_date_start(page, "Date"),
            summary=_read_rich_text(page, "會議摘要"),
            content=content,
            artist_page_ids=artists,
            task_page_ids=task_ids,
            project_page_ids=project_ids,
            attachments=tuple(attachments),
        ))
    return records


async def get_course_meeting_summaries() -> list[CourseMeetingRecord]:
    """管理端 AI 使用的輕量清單；不下載頁面附件或正文。"""
    pages = await notion_api.query_data_source(
        config.DS_COURSE_MEETINGS,
        sorts=[{"property": "Date", "direction": "descending"}],
    )
    return [CourseMeetingRecord(
        page_id=page["id"],
        url=page.get("url", ""),
        title=_read_title(page, "Name"),
        date=_read_date_start(page, "Date"),
        summary=_read_rich_text(page, "會議摘要"),
        content="",
        artist_page_ids=tuple(_read_relation_ids(page, "參與藝人")),
        task_page_ids=(),
        project_page_ids=tuple(_read_relation_ids(page, "關聯專案")),
    ) for page in pages if not page.get("archived")]


async def create_bulletin_feedback(
    source: BulletinItem,
    discord_user: str,
    discord_user_id: int,
    feedback: str,
    discord_message_url: str = "",
) -> dict:
    """將 Discord 公告回覆寫回同一個 Notion 布告欄資料庫。"""
    now = datetime.now(config.TZ)
    title = f"回饋｜{source.title}｜{discord_user}"[:2000]
    body_text = (
        f"回覆者：{discord_user}（Discord ID：{discord_user_id}）\n"
        f"原公告：{source.title}\n"
        f"原公告 Notion：{source.url}\n"
        f"Discord 回覆：{discord_message_url or '未提供'}\n\n"
        f"回饋內容：\n{feedback.strip()}"
    )
    children = [{
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [{
                "type": "text",
                "text": {"content": body_text[:2000]},
            }],
        },
    }]
    props = {
        "Name": _title(title),
        "Date": _date(now),
        "類別": _select("🙋 使用回饋"),
        "狀態": _select("未處理"),
    }
    return await notion_api.create_page(config.DS_BULLETIN, props, children=children)


def build_ai_feedback_page(
    *,
    artist_name: str,
    discord_user: str,
    discord_user_id: int,
    feedback: str,
    answer_message_url: str = "",
) -> tuple[dict, list[dict]]:
    """建立課程助教回報資料；刻意不保存原始問題與完整回答。"""
    now = datetime.now(config.TZ)
    body_text = (
        f"藝人：{artist_name}\n"
        f"回報者：{discord_user}（Discord ID：{discord_user_id}）\n"
        f"Discord 回答：{answer_message_url or '未提供'}\n\n"
        f"回報內容：\n{feedback.strip()}"
    )
    props = {
        "Name": _title(f"AI 回報｜{artist_name}｜{now:%Y-%m-%d %H:%M}"[:2000]),
        "Date": _date(now),
        "類別": _select("🙋 使用回饋"),
        "狀態": _select("未處理"),
    }
    children = [{
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [{
                "type": "text",
                "text": {"content": body_text[:2000]},
            }],
        },
    }]
    return props, children


async def create_ai_feedback(**kwargs) -> dict:
    props, children = build_ai_feedback_page(**kwargs)
    return await notion_api.create_page(config.DS_BULLETIN, props, children=children)


# ---------- Artists ----------

@dataclass
class Artist:
    page_id: str
    name: str
    discord_user_id: str
    status: str | None = None  # KariKari schema: 合約狀態
    drive_folder_url: str = ""  # 藝人專區資料夾連結（作業歸檔基底）


def _to_artist(page: dict) -> Artist:
    return Artist(
        page_id=page["id"],
        name=_read_title(page, "藝人名稱"),
        discord_user_id=_read_rich_text(page, "Discord User ID").strip(),
        status=_read_select(page, "合約狀態"),
        drive_folder_url=_read_url(page, "Google Drive 資料夾"),
    )


INACTIVE_ARTIST_STATUSES = {"未簽", "暫停", "封存"}


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


async def get_teachers() -> list[str]:
    """讀 People 中身份類型為「老師」的人名，供 Discord 選單使用。"""
    return [name for _, name in await get_teacher_options()]


async def get_teacher_options() -> list[tuple[str, str]]:
    """回傳可授課 People 的 (page_id, name)，供 Task Relation 選擇使用。"""
    pages = await notion_api.query_data_source(
        config.DS_PEOPLE,
        filter_={"or": [
            {"property": "身份類型", "select": {"equals": "老師"}},
            {"property": "身份類型", "select": {"equals": "公司成員"}},
        ]},
    )
    options = [
        (page["id"], _read_title(page, "姓名 / 名稱").strip())
        for page in pages
        if _read_select(page, "身份類型") in {"老師", "公司成員"}
    ]
    return sorted({option for option in options if option[1]}, key=lambda option: option[1])


async def get_task_type_options() -> list[str]:
    """讀取 Tasks 的「任務類型」select 選項，避免把租戶分類寫死在 Bot。"""
    schema = await notion_api.retrieve_data_source(config.DS_TASKS)
    property_schema = schema.get("properties", {}).get("任務類型", {})
    options = property_schema.get("select", {}).get("options", [])
    return [option["name"].strip() for option in options if option.get("name", "").strip()]


@dataclass(frozen=True)
class PersonItem:
    page_id: str
    name: str
    discord_user_id: str = ""
    identity_type: str | None = None


async def get_people_items_by_ids(page_ids: list[str]) -> list[PersonItem]:
    """讀取指定 People；沒有 Discord ID 的人仍保留名稱供人工通知。"""
    if not page_ids:
        return []
    pages = await notion_api.query_data_source(config.DS_PEOPLE)
    wanted = set(page_ids)
    return [
        PersonItem(
            page_id=page["id"],
            name=_read_title(page, "姓名 / 名稱").strip() or "未命名人員",
            discord_user_id=_read_rich_text(page, "Discord User ID").strip(),
            identity_type=_read_select(page, "身份類型"),
        )
        for page in pages
        if page["id"] in wanted
    ]


async def get_people_by_ids(page_ids: list[str]) -> list[PersonItem]:
    """讀取指定公司成員，供審核人員提醒使用。"""
    return [
        person for person in await get_people_items_by_ids(page_ids)
        if person.identity_type == "公司成員"
    ]


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
    project_type: str | None = None
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
    course_category: str | None = None
    course_meeting_page_ids: list[str] = field(default_factory=list)
    teacher_page_ids: list[str] = field(default_factory=list)
    reviewer_page_ids: list[str] = field(default_factory=list)
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
    # P0-3 歸檔用：任務指定資料夾（優先於藝人專區）
    drive_folder_url: str = ""
    # Notion page 建立時間，用於沒有截止日的 Drive 資料夾命名
    created_at: datetime | None = None
    # 任務派發通知／收件確認
    discord_notified_at: datetime | None = None
    discord_acknowledged_at: datetime | None = None
    review_deadline: datetime | None = None
    revision_deadline: datetime | None = None


@dataclass
class SubmissionItem:
    page_id: str
    name: str
    status: str | None
    artist_page_ids: list[str] = field(default_factory=list)
    task_page_ids: list[str] = field(default_factory=list)
    latest: bool = False
    submit_type: str | None = None
    drive_url: str = ""
    note: str = ""
    review_feedback: str = ""
    reviewer: str = ""
    review_source: str | None = None
    review_notified_at: datetime | None = None
    url: str = ""
    created_at: datetime | None = None


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
    start = _read_date_value(page, "開始日")
    end = _read_date_value(page, "截止日")
    return ProjectItem(
        page_id=page["id"],
        name=_read_title(page, "專案名稱"),
        status=_read_select(page, "專案狀態"),
        project_type=_read_select(page, "專案類型"),
        artist_page_ids=_read_relation_ids(page, "關聯藝人"),
        start=start.value,
        end=end.value,
        start_has_time=start.has_time,
        end_has_time=end.has_time,
    )


def _to_task(page: dict) -> TaskItem:
    start = _read_date_value(page, "開始日")
    end = _read_date_value(page, "截止日")
    created_at_raw = page.get("created_time")
    created_at = None
    if created_at_raw:
        try:
            created_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00")).astimezone(config.TZ)
        except ValueError:
            created_at = None
    return TaskItem(
        page_id=page["id"],
        name=_read_title(page, "任務名稱"),
        status=_read_select(page, "任務狀態"),
        task_type=_read_select(page, "任務類型"),
        course_category=_read_select(page, "課程類別"),
        course_meeting_page_ids=_read_relation_ids(page, "課程與會議記錄"),
        teacher_page_ids=_read_relation_ids(page, "負責老師"),
        reviewer_page_ids=_read_relation_ids(page, "審核人員"),
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
        drive_folder_url=_read_url(page, "Google Drive Folder"),
        created_at=created_at,
        discord_notified_at=_read_date_start(page, "Discord 通知時間"),
        discord_acknowledged_at=_read_date_start(page, "Discord 收到時間"),
        review_deadline=_read_date_start(page, "審核期限"),
        revision_deadline=_read_date_start(page, "修改期限"),
    )


def _to_submission(page: dict) -> SubmissionItem:
    return SubmissionItem(
        page_id=page["id"],
        name=_read_title(page, "提交名稱"),
        status=_read_select(page, "審核狀態"),
        artist_page_ids=_read_relation_ids(page, "提交藝人"),
        task_page_ids=_read_relation_ids(page, "關聯任務"),
        latest=_read_checkbox(page, "是否最新版"),
        submit_type=_read_select(page, "提交類型"),
        drive_url=_read_url(page, "Drive URL"),
        note=_read_rich_text(page, "備註"),
        review_feedback=_read_rich_text(page, "審核回饋"),
        reviewer=_read_rich_text(page, "審核者"),
        review_source=_read_select(page, "審核來源"),
        review_notified_at=_read_date_start(page, "審核通知時間"),
        url=page.get("url", ""),
        created_at=_parse_notion_datetime(page.get("created_time")).value
        if page.get("created_time") else None,
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
        (
            task.task_type in HARD_BLOCK_TASK_TYPES
            or (task.task_type == "課程與作業" and bool(task.course_category))
        )
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


def split_windows_by_duration(
    windows: list[tuple[datetime, datetime]], duration_minutes: int
) -> list[tuple[datetime, datetime]]:
    """把可用長窗切成不重疊、連續的固定時長候選。"""
    if duration_minutes <= 0:
        return windows
    duration = timedelta(minutes=duration_minutes)
    result: list[tuple[datetime, datetime]] = []
    for start, end in windows:
        cursor = start
        while cursor + duration <= end:
            result.append((cursor, cursor + duration))
            cursor += duration
    return result


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


async def get_all_projects() -> list[ProjectItem]:
    pages = await notion_api.query_data_source(config.DS_PROJECTS)
    return [_to_project(page) for page in pages if not page.get("archived")]


async def get_course_training_projects() -> list[ProjectItem]:
    """供時間池課程安排選擇的進行中「課程培訓」專案。"""
    pages = await notion_api.query_data_source(config.DS_PROJECTS)
    projects = [_to_project(page) for page in pages]
    return sorted(
        [
            project for project in projects
            if project.project_type == "課程培訓"
            and project.status not in INACTIVE_PROJECT_STATUSES
        ],
        key=lambda project: project.name,
    )


async def get_tasks_for_artist(artist_page_id: str) -> list[TaskItem]:
    project_map = await get_project_map()
    pages = await notion_api.query_data_source(config.DS_TASKS)
    tasks = [_to_task(page) for page in pages]
    return [
        task for task in tasks
        if task_applies_to_artist(task, artist_page_id, project_map)
    ]


async def get_task(task_page_id: str) -> TaskItem | None:
    """Retrieve one task for submission-linked Drive archive paths."""
    if not task_page_id:
        return None
    page = await notion_api.retrieve_page(task_page_id)
    return _to_task(page) if page else None


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


async def get_all_tasks() -> list[TaskItem]:
    pages = await notion_api.query_data_source(config.DS_TASKS)
    return [_to_task(page) for page in pages if not page.get("archived")]


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


async def mark_task_discord_notified(
    page_id: str,
    notified_at: datetime | None = None,
) -> None:
    """記錄任務已發送 Discord 派發通知。"""
    await notion_api.update_page(
        page_id,
        {"Discord 通知時間": _date(notified_at or datetime.now(config.TZ))},
    )


async def reset_task_discord_notification(page_id: str) -> None:
    """清除通知／收到時間，供管理者要求重新派發測試或補發通知。"""
    await notion_api.update_page(
        page_id,
        {
            "Discord 通知時間": {"date": None},
            "Discord 收到時間": {"date": None},
        },
    )


async def acknowledge_task(
    page_id: str,
    acknowledged_at: datetime | None = None,
) -> bool:
    """回寫藝人收到任務；回傳 False 代表舊 schema 沒有收到時間欄位。"""
    acknowledged_at = acknowledged_at or datetime.now(config.TZ)
    props = {
        "任務狀態": _select("進行中"),
        "Discord 收到時間": _date(acknowledged_at),
    }
    try:
        await notion_api.update_page(page_id, props)
        return True
    except NotionAPIError as exc:
        if "Discord 收到時間" not in str(exc):
            raise
        # 讓既有未加欄位的租戶仍可使用按鈕；收到時間會保留在 Audit Log。
        await notion_api.update_page(page_id, {"任務狀態": _select("進行中")})
        return False


async def record_task_response(
    page_id: str,
    responded_at: datetime | None = None,
) -> bool:
    """記錄藝人已回應任務；不改變任務狀態，供有問題回饋使用。"""
    responded_at = responded_at or datetime.now(config.TZ)
    try:
        await notion_api.update_page(page_id, {"Discord 收到時間": _date(responded_at)})
        return True
    except NotionAPIError as exc:
        if "Discord 收到時間" not in str(exc):
            raise
        return False


async def append_task_feedback(page_id: str, entry: str) -> TaskItem:
    """將藝人回饋追加到既有的 Task 任務說明，不新增 Notion 欄位。"""
    task = await get_task(page_id)
    if task is None:
        raise ValueError(f"Task not found: {page_id}")
    description = f"{task.description.strip()}\n\n{entry.strip()}".strip()
    await notion_api.update_page(page_id, {"任務說明": _rich_text(description)})
    return await get_task(page_id) or task


# Backward-compatible alias for internal callers and older integrations.
append_task_note = append_task_feedback


def _build_formal_arrangement_task_props(
    name: str,
    task_type: str,
    artist_page_ids: list[str],
    description: str,
    start: datetime | None = None,
    end: datetime | None = None,
    course_category: str | None = None,
    teacher_page_ids: list[str] | None = None,
    project_page_ids: list[str] | None = None,
    discord_visible: bool = True,
) -> dict:
    props = {
        "任務名稱": _title(name),
        "任務狀態": _select("尚未開始"),
        "任務類型": _select(task_type),
        "關聯藝人": _relation(artist_page_ids),
        "任務說明": _rich_text(description),
        "是否開放提交": _checkbox(False),
        "Discord 顯示": _checkbox(discord_visible),
    }
    if course_category:
        props["課程類別"] = _select(course_category)
    if teacher_page_ids:
        props["負責老師"] = _relation(teacher_page_ids)
    if project_page_ids:
        props["關聯專案"] = _relation(project_page_ids)
    if start:
        props["開始日"] = _date(start)
    if end:
        props["截止日"] = _date(end)
    return props


async def create_formal_arrangement_task(
    name: str,
    task_type: str,
    artist_page_ids: list[str],
    description: str,
    start: datetime | None = None,
    end: datetime | None = None,
    course_category: str | None = None,
    teacher_page_ids: list[str] | None = None,
    project_page_ids: list[str] | None = None,
    discord_visible: bool = True,
) -> TaskItem:
    """建立由時間池確認出的正式／待確認安排 Task。"""
    props = _build_formal_arrangement_task_props(
        name=name,
        task_type=task_type,
        artist_page_ids=artist_page_ids,
        description=description,
        start=start,
        end=end,
        course_category=course_category,
        teacher_page_ids=teacher_page_ids,
        project_page_ids=project_page_ids,
        discord_visible=discord_visible,
    )
    page = await notion_api.create_page(config.DS_TASKS, props)
    return _to_task(page)


def is_publishable_formal_task(task: TaskItem) -> bool:
    """正式通告候選必須是未結案、具精準時間且已關聯藝人的 Task。

    任務類型由租戶的 Tasks schema 決定；拍攝、行政或其他自訂分類也可能
    是控方要發布的正式通告，因此這裡不再硬編碼只接受硬性佔用類型。
    """
    return bool(
        task.artist_page_ids
        and _is_active_task(task)
        and task.start
        and task.end
        and task.start_has_time
        and task.end_has_time
        and task.end > task.start
    )


def task_description_field(description: str, *field_names: str) -> str:
    for line in description.splitlines():
        stripped = line.strip()
        for field_name in field_names:
            prefix = f"{field_name}："
            if stripped.startswith(prefix):
                return stripped[len(prefix):].strip()
    return ""


async def mark_task_formally_published(
    page_id: str,
    notified_at: datetime | None = None,
) -> None:
    """正式發布後開放 Discord 顯示，並避免背景派發再送一次新任務通知。"""
    await notion_api.update_page(
        page_id,
        {
            "Discord 顯示": _checkbox(True),
            "Discord 通知時間": _date(notified_at or datetime.now(config.TZ)),
        },
    )


# ---------- P0-3 作業提交（Submissions & File Index） ----------

# Submissions.檔案類型 的 select 選項對應任務類型；對不上的一律「其他」
SUBMISSION_FILE_TYPE_BY_TASK_TYPE = {
    # Tasks 已統一為「課程與作業」；Submissions 的檔案類型先維持舊選項，
    # 避免為了改名而批次改動歷史提交紀錄。
    "課程與作業": "課程作業",
    "課程作業": "課程作業",
    "創作交付": "Demo",
    "企劃提案": "企劃書",
    "業配內容": "業配素材",
    "錄音": "錄音檔",
}

SUBMITTABLE_TASK_STATUSES = {"尚未開始", "進行中", "待提交", "需修改", "已逾期"}


def submission_file_type_for_task(task_type: str | None) -> str:
    return SUBMISSION_FILE_TYPE_BY_TASK_TYPE.get(task_type or "", "其他")


def is_task_submittable(task: TaskItem) -> bool:
    """可提交＝任務開放提交且還沒進入審核/結案狀態（待審核可重交由審核流退回）。"""
    if not task.open_for_submission:
        return False
    if task.status is None:
        return True
    return task.status in SUBMITTABLE_TASK_STATUSES


def _build_submission_props(
    task: TaskItem,
    artist: Artist,
    *,
    submit_type: str,  # 「檔案」「Drive 連結」「文字」「混合」
    link_url: str = "",
    content: str = "",
    drive_file_id: str = "",
    original_filename: str = "",
    file_size_mb: float | None = None,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(config.TZ)
    name = f"{artist.name}｜{task.name}"[:80] + f"｜{now.strftime('%m/%d %H:%M')}"
    props = {
        "提交名稱": _title(name),
        "提交藝人": _relation([artist.page_id]),
        "關聯任務": _relation([task.page_id]),
        "提交來源": _select("Discord"),
        "提交類型": _select(submit_type),
        "檔案類型": _select(submission_file_type_for_task(task.task_type)),
        "提交時間": _date(now),
        "是否最新版": _checkbox(True),
        "審核狀態": _select("待審核"),
        "權限等級": _select("藝人本人"),
    }
    if link_url:
        props["Drive URL"] = _url(link_url)
    if content:
        props["備註"] = _rich_text(content)
    if drive_file_id:
        props["Drive File ID"] = _rich_text(drive_file_id)
    if original_filename:
        props["原始檔名"] = _rich_text(original_filename)
    if file_size_mb is not None:
        props["檔案大小 MB"] = _number(file_size_mb)
    return props


async def create_submission(
    task: TaskItem,
    artist: Artist,
    *,
    submit_type: str,
    link_url: str = "",
    content: str = "",
    drive_file_id: str = "",
    original_filename: str = "",
    file_size_mb: float | None = None,
) -> dict:
    props = _build_submission_props(
        task, artist, submit_type=submit_type, link_url=link_url, content=content,
        drive_file_id=drive_file_id, original_filename=original_filename,
        file_size_mb=file_size_mb,
    )
    return await notion_api.create_page(config.DS_SUBMISSIONS, props)


async def mark_previous_submissions_not_latest(task_page_id: str) -> int:
    """同一任務的舊提交取消「是否最新版」，回傳處理筆數。"""
    pages = await notion_api.query_data_source(
        config.DS_SUBMISSIONS,
        filter_={"and": [
            {"property": "關聯任務", "relation": {"contains": task_page_id}},
            {"property": "是否最新版", "checkbox": {"equals": True}},
        ]},
    )
    for page in pages:
        await notion_api.update_page(page["id"], {"是否最新版": _checkbox(False)})
    return len(pages)


async def set_task_under_review(task_page_id: str, latest_url: str) -> None:
    """提交完成：任務轉待審核並記最新提交 URL。"""
    props: dict = {"任務狀態": _select("待審核")}
    if latest_url:
        props["最新提交 URL"] = _url(latest_url)
    # 審核期限是提交後 24 小時；舊租戶尚未建欄位時保留原本提交流程。
    props["審核期限"] = _date(datetime.now(config.TZ) + timedelta(hours=24))
    try:
        await notion_api.update_page(task_page_id, props)
    except NotionAPIError as exc:
        if "審核期限" not in str(exc):
            raise
        props.pop("審核期限", None)
        await notion_api.update_page(task_page_id, props)


async def set_task_revision_deadline(
    task_page_id: str,
    deadline: datetime | None,
) -> None:
    """設定或清除需修改期限；舊租戶沒有欄位時不阻斷審核結果寫入。"""
    try:
        await notion_api.update_page(
            task_page_id,
            {"修改期限": _date(deadline) if deadline else {"date": None}},
        )
    except NotionAPIError as exc:
        if "修改期限" not in str(exc):
            raise


def compute_review_deadline_stage(task: TaskItem, now: datetime) -> str | None:
    """回傳審核期限提醒階段；只處理尚未完成的待審核任務。"""
    if task.status != "待審核" or not task.review_deadline:
        return None
    if task.review_deadline <= now:
        return "審核逾時"
    if task.review_deadline - now <= timedelta(hours=6):
        return "審核期限將到"
    return None


def compute_revision_deadline_stage(task: TaskItem, now: datetime) -> str | None:
    """回傳藝人修改期限提醒階段。"""
    if task.status != "需修改" or not task.revision_deadline:
        return None
    if task.revision_deadline <= now:
        return "修改期限逾時"
    if task.revision_deadline - now <= timedelta(hours=24):
        return "修改期限將到"
    return None


# ---------- P0-5 審核通知（Submissions → Tasks → Discord DM） ----------

REVIEW_RESULT_STATUSES = ("通過", "需修改", "退回")


def review_update_props(
    *,
    status: str,
    feedback: str = "",
    reviewer: str = "",
    source: str = "",
    notified_at: datetime | None = None,
) -> dict:
    props: dict = {"審核狀態": _select(status)}
    if feedback:
        props["審核回饋"] = _rich_text(feedback)
    if reviewer:
        props["審核者"] = _rich_text(reviewer)
    if source:
        props["審核來源"] = _select(source)
    if notified_at:
        props["審核通知時間"] = _date(notified_at)
    return props


async def get_submissions_by_review_status(
    statuses: list[str] | tuple[str, ...],
    *,
    latest_only: bool = True,
) -> list[SubmissionItem]:
    status_filters = [
        {"property": "審核狀態", "select": {"equals": status}}
        for status in statuses
    ]
    filters: list[dict] = [{"or": status_filters}] if len(status_filters) > 1 else status_filters
    if latest_only:
        filters.append({"property": "是否最新版", "checkbox": {"equals": True}})
    pages = await notion_api.query_data_source(config.DS_SUBMISSIONS, filter_={"and": filters})
    return [_to_submission(page) for page in pages]


async def get_reviewable_submissions() -> list[SubmissionItem]:
    return await get_submissions_by_review_status(["待審核"], latest_only=True)


async def get_all_submissions() -> list[SubmissionItem]:
    pages = await notion_api.query_data_source(config.DS_SUBMISSIONS)
    return [_to_submission(page) for page in pages if not page.get("archived")]


def review_submission_ready_for_auto_notify(submission: SubmissionItem) -> bool:
    """Notion 端手動審核通知的保護條件。

    只改 `審核狀態` 可能是歷史資料或整理資料，不一定代表老師剛審完。
    需要至少有回饋、審核者或審核來源其中一項，才由背景 loop 推送給藝人。
    Discord `/審核提交` 會直接寫 `審核來源=Discord`，不受影響。
    """
    return bool(
        submission.review_feedback.strip()
        or submission.reviewer.strip()
        or submission.review_source
    )


async def get_pending_review_notifications() -> list[SubmissionItem]:
    submissions = await get_submissions_by_review_status(REVIEW_RESULT_STATUSES, latest_only=True)
    return [
        submission for submission in submissions
        if submission.review_notified_at is None
        and review_submission_ready_for_auto_notify(submission)
    ]



async def update_submission_review(
    page_id: str,
    *,
    status: str,
    feedback: str = "",
    reviewer: str = "",
    source: str = "",
    notified_at: datetime | None = None,
) -> None:
    await notion_api.update_page(
        page_id,
        review_update_props(
            status=status,
            feedback=feedback,
            reviewer=reviewer,
            source=source,
            notified_at=notified_at,
        ),
    )


async def mark_submission_review_notified(
    page_id: str,
    *,
    source: str = "Notion",
    notified_at: datetime | None = None,
) -> None:
    notified_at = notified_at or datetime.now(config.TZ)
    await notion_api.update_page(
        page_id,
        {
            "審核來源": _select(source),
            "審核通知時間": _date(notified_at),
        },
    )


# 「撞課」情境（Tasks/Projects）每次填時段都要算，且含兩次全表掃描。
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
    context = ScheduleImpactContext(
        busy_intervals=busy_intervals_for_tasks(tasks),
        warnings=warnings_for_tasks(tasks) + warnings_for_projects(projects),
    )
    _impact_context_cache[artist_page_id] = (now, context)
    return context


# ---------- Availability Slots ----------

@dataclass
class AvailabilitySlot:
    page_id: str
    name: str
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
        artist_page_ids=_read_relation_ids(page, "填寫藝人"),
        start=_read_date_start(page, "可用開始時間"),
        end=_read_date_start(page, "可用結束時間"),
        note=_read_rich_text(page, "備註"),
    )


def _slot_scope(slot: AvailabilitySlot) -> tuple[str, ...]:
    return tuple(sorted(slot.artist_page_ids))


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
    artist_page_id: str,
    start: datetime,
    end: datetime,
) -> AvailabilitySlot | None:
    """Find an existing active slot for the same artist and exact time window."""
    for slot in slots:
        if artist_page_id not in slot.artist_page_ids:
            continue
        if slot.start == start and slot.end == end:
            return slot
    return None


def _build_availability_slot_props(
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
    if note:
        props["備註"] = _rich_text(note)
    return props


async def create_availability_slot(
    artist: Artist,
    start: datetime,
    end: datetime,
    is_late: bool,
    note: str = "",
) -> AvailabilitySlot:
    props = _build_availability_slot_props(artist, start, end, is_late, note)
    page = await notion_api.create_page(config.DS_AVAILABILITY_SLOTS, props)
    return _to_slot(page)


async def create_availability_slot_if_absent(
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
        artist_page_id=artist.page_id,
        start=start,
        end=end,
    )
    if duplicate is not None:
        return duplicate, False
    created = await create_availability_slot(artist, start, end, is_late, note)
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

    Each selected artist's slots are split by formal busy intervals before the
    all-person intersection is calculated.
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
