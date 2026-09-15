"""Helpers for month-based availability collection UI."""
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import config
from services import timeparse

logger = logging.getLogger(__name__)


WEEKDAY_ZH = "一二三四五六日"


@dataclass(frozen=True)
class WeekOption:
    label: str
    value: str
    start: date
    end: date


@dataclass(frozen=True)
class DayOption:
    label: str
    value: str
    day: date


@dataclass(frozen=True)
class PresetWindow:
    label: str
    value: str
    start: time
    end: time


@dataclass(frozen=True)
class AvailabilityMode:
    label: str
    value: str
    description: str


@dataclass(frozen=True)
class CollectionNature:
    label: str
    value: str
    description: str
    announcement: str


@dataclass(frozen=True)
class ActiveCollection:
    month: str
    deadline_ts: int
    artist_page_ids: tuple[str, ...]


DEFAULT_DAY_START = time(8, 0)
DEFAULT_DAY_END = time(0, 0)  # 次日 00:00，對使用者顯示為當日 24:00
DEFAULT_COLLECTION_NATURE = "monthly_reference"
DISCORD_SELECT_OPTION_LIMIT = 25
MONTH_DATE_PAGE_SIZE = 16
MONTHLY_REFERENCE_LABEL = "月度可配合時段"
LEGACY_COLLECTION_NATURE_VALUES = {"direct", "confirm"}
LEGACY_COLLECTION_NATURE_LABELS = {"可直接安排", "待確認"}

PRESET_WINDOWS = [
    PresetWindow("上午 08:00-12:00", "08:00-12:00", time(8, 0), time(12, 0)),
    PresetWindow("下午 14:00-18:00", "14:00-18:00", time(14, 0), time(18, 0)),
    PresetWindow("晚上 19:00-24:00", "19:00-24:00", time(19, 0), time(0, 0)),
    PresetWindow("整段 08:00-24:00", "08:00-24:00", time(8, 0), time(0, 0)),
]

AVAILABILITY_MODES = [
    AvailabilityMode("整天可", "all_day", "08:00-24:00 都可安排"),
    AvailabilityMode("某時間後可", "after", "例：13:30 後都可安排"),
    AvailabilityMode("某時間前可", "before", "例：17:00 前都可安排"),
    AvailabilityMode("指定時段", "specific", "例：14:00-18:00"),
]

COLLECTION_NATURES = [
    CollectionNature(
        label=MONTHLY_REFERENCE_LABEL,
        value=DEFAULT_COLLECTION_NATURE,
        description="供營運初步排程參考，正式安排前仍會再確認",
        announcement=(
            "請回報大概可配合的時間，營運會用來初步安排課程、錄音或通告。"
            "這不是正式預約，正式安排前會再確認。"
        ),
    ),
]

# 進行中收集與已發通知會持久化到本地狀態檔：bot 重啟後催填／收齊通知不中斷、
# 也不重複發送。這是 bot 執行期狀態、不是營運資料，所以放本地 JSON 不進 Notion。
# 多租戶時改為每租戶一個狀態檔（路徑帶 tenant name）。
_STATE_PATH = Path(".state/time_collection.json")

_active_collections: dict[tuple[str, int], ActiveCollection] = {}
_sent_collection_notices: set[tuple[str, int, str]] = set()
_state_loaded = False


def _prune_key(today: date) -> str:
    """保留本月與未來的收集；上上個月以前的直接丟棄。"""
    prev = today.replace(day=1) - timedelta(days=1)
    return f"{prev.year:04d}-{prev.month:02d}"


def _load_state() -> None:
    global _state_loaded
    if _state_loaded:
        return
    _state_loaded = True
    try:
        raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return
    keep_from = _prune_key(datetime.now(config.TZ).date())
    for item in raw.get("collections", []):
        month = item.get("month", "")
        if month < keep_from:
            continue
        collection = ActiveCollection(
            month=month,
            deadline_ts=int(item.get("deadline_ts", 0)),
            artist_page_ids=tuple(item.get("artist_page_ids", [])),
        )
        _active_collections[(collection.month, collection.deadline_ts)] = collection
    for item in raw.get("sent_notices", []):
        if len(item) == 3 and item[0] >= keep_from:
            _sent_collection_notices.add((item[0], int(item[1]), item[2]))


def _save_state() -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "collections": [
                {
                    "month": c.month,
                    "deadline_ts": c.deadline_ts,
                    "artist_page_ids": list(c.artist_page_ids),
                }
                for c in _active_collections.values()
            ],
            "sent_notices": [list(key) for key in _sent_collection_notices],
        }
        _STATE_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError:
        logger.exception("時間收集狀態檔寫入失敗（提醒重啟後可能中斷）")


def register_active_collection(
    month: str,
    deadline: datetime,
    artist_page_ids: list[str],
) -> ActiveCollection:
    _load_state()
    normalized_month = normalize_month(month)
    if normalized_month is None:
        raise ValueError(f"Invalid month: {month}")
    collection = ActiveCollection(
        month=normalized_month,
        deadline_ts=int(deadline.timestamp()),
        artist_page_ids=tuple(dict.fromkeys(artist_page_ids)),
    )
    _active_collections[(collection.month, collection.deadline_ts)] = collection
    _save_state()
    return collection


def deregister_collection(month: str, deadline_ts: int) -> None:
    """收集進入終態（收齊或逾期通知已發）後移除，避免提醒 loop 每輪繼續查詢。"""
    _load_state()
    _active_collections.pop((month, int(deadline_ts)), None)
    _save_state()


def active_collections() -> list[ActiveCollection]:
    _load_state()
    return sorted(_active_collections.values(), key=lambda item: (item.month, item.deadline_ts))


def collection_notice_once(month: str, deadline_ts: int, stage: str) -> bool:
    _load_state()
    normalized_month = normalize_month(month)
    if normalized_month is None:
        raise ValueError(f"Invalid month: {month}")
    key = (normalized_month, int(deadline_ts), stage)
    if key in _sent_collection_notices:
        return False
    _sent_collection_notices.add(key)
    _save_state()
    return True


def clear_active_collections_for_tests() -> None:
    global _state_loaded
    _active_collections.clear()
    _sent_collection_notices.clear()
    _state_loaded = False


def reload_state_for_tests() -> None:
    """模擬 bot 重啟：清掉記憶體、下次存取時從狀態檔重載。"""
    global _state_loaded
    _active_collections.clear()
    _sent_collection_notices.clear()
    _state_loaded = False


def availability_announcement_copy(month: str) -> str:
    return (
        "請提供這個月方便安排的時段，\n"
        "供 LURASUPA 行政安排課程、錄音或通告參考。\n"
        "正式安排前，LURASUPA 行政會再向你確認。\n\n"
        "請點選「選擇可配合時段」開始填寫。"
    )


def availability_selection_sop() -> str:
    return (
        "**填寫方式**\n"
        "1. 勾選日期（每頁約一半月份）；需要時按「下一頁」。\n"
        "2. 選時間方式，會套用到已選日期。\n"
        "   ・整天可：08:00–24:00\n"
        "   ・某時間後：輸入開始時間\n"
        "   ・某時間前：輸入結束時間\n"
        "   ・指定時段：輸入開始與結束時間\n"
        "3. 不同時間就分批選；填完按「完成」。"
    )


def availability_page_hint(selected_day_count: int = 0, selected_mode: str | None = None) -> str:
    if selected_day_count <= 0:
        return "先勾日期；同一批日期會套用同一個時間。"
    if not selected_mode:
        return f"已選 {selected_day_count} 天，再選時間。"
    return f"已選 {selected_day_count} 天，可送出；其他日期時間不同再分下一組。"


def note_field(note: str, field_name: str) -> str | None:
    prefix = f"{field_name}："
    for part in note.split("｜"):
        part = part.strip()
        if part.startswith(prefix):
            return part[len(prefix):].strip() or None
    return None


def format_slot_summary(
    start: datetime,
    end: datetime,
    note: str = "",
    *,
    include_nature: bool = True,
) -> str:
    weekday = WEEKDAY_ZH[start.weekday()]
    same_day = start.date() == end.date()
    ends_at_midnight = end.time() == time(0, 0) and end.date() == start.date() + timedelta(days=1)
    if same_day:
        time_text = f"{start:%H:%M}-{end:%H:%M}"
    elif ends_at_midnight:
        time_text = f"{start:%H:%M}-24:00"
    else:
        time_text = f"{start:%H:%M}-{end.month}/{end.day} {end:%H:%M}"
    nature = normalize_collection_nature_label(note_field(note, "收集性質")) if include_nature else None
    suffix = f"｜{nature}" if nature else ""
    return f"{start.month}/{start.day}（週{weekday}） {time_text}{suffix}"


def collection_nature(value: str) -> CollectionNature:
    if value in LEGACY_COLLECTION_NATURE_VALUES:
        value = DEFAULT_COLLECTION_NATURE
    for nature in COLLECTION_NATURES:
        if nature.value == value:
            return nature
    raise ValueError(f"Unknown collection nature: {value}")


def collection_nature_label(value: str) -> str:
    return collection_nature(value).label


def normalize_collection_nature_label(label: str | None) -> str | None:
    if label in LEGACY_COLLECTION_NATURE_LABELS:
        return MONTHLY_REFERENCE_LABEL
    return label


def normalize_month(month: str) -> str | None:
    month = month.strip().replace("／", "/").replace("/", "-")
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", month)
    if not m:
        return None
    year, month_num = int(m.group(1)), int(m.group(2))
    if not 1 <= month_num <= 12:
        return None
    return f"{year:04d}-{month_num:02d}"


def parse_month(month: str) -> tuple[int, int]:
    normalized = normalize_month(month)
    if normalized is None:
        raise ValueError(f"Invalid month: {month}")
    year_text, month_text = normalized.split("-")
    return int(year_text), int(month_text)


def month_week_options(month: str) -> list[WeekOption]:
    year, month_num = parse_month(month)
    first = date(year, month_num, 1)
    next_month = date(year + (month_num // 12), (month_num % 12) + 1, 1)
    last = next_month - timedelta(days=1)

    options: list[WeekOption] = []
    start = first
    while start <= last:
        end = min(start + timedelta(days=6), last)
        label = f"{start.month}/{start.day}-{end.month}/{end.day}"
        value = f"{start.isoformat()}:{end.isoformat()}"
        options.append(WeekOption(label=label, value=value, start=start, end=end))
        start = end + timedelta(days=1)
    return options


def week_day_options(
    week_value: str,
    excluded_dates: set[date] | None = None,
) -> list[DayOption]:
    excluded_dates = excluded_dates or set()
    start_text, end_text = week_value.split(":", 1)
    start = date.fromisoformat(start_text)
    end = date.fromisoformat(end_text)
    options: list[DayOption] = []
    current = start
    while current <= end:
        if current in excluded_dates:
            current += timedelta(days=1)
            continue
        weekday = WEEKDAY_ZH[current.weekday()]
        label = f"{current.month}/{current.day}（週{weekday}）"
        options.append(DayOption(label=label, value=current.isoformat(), day=current))
        current += timedelta(days=1)
    return options


def week_values_day_options(
    week_values: list[str],
    excluded_dates: set[date] | None = None,
) -> list[DayOption]:
    options_by_value: dict[str, DayOption] = {}
    for week_value in week_values:
        for option in week_day_options(week_value, excluded_dates=excluded_dates):
            options_by_value[option.value] = option
    return sorted(options_by_value.values(), key=lambda option: option.day)


def day_option_pages(
    week_values: list[str],
    page_size: int = MONTH_DATE_PAGE_SIZE,
    excluded_dates: set[date] | None = None,
) -> list[list[DayOption]]:
    options = week_values_day_options(week_values, excluded_dates=excluded_dates)
    if not options:
        return []
    page_count = max(1, (len(options) + page_size - 1) // page_size)
    base_size, extra = divmod(len(options), page_count)
    pages: list[list[DayOption]] = []
    cursor = 0
    for page_index in range(page_count):
        current_size = base_size + (1 if page_index < extra else 0)
        pages.append(options[cursor:cursor + current_size])
        cursor += current_size
    return pages


def preset_window(value: str) -> PresetWindow:
    for preset in PRESET_WINDOWS:
        if preset.value == value:
            return preset
    raise ValueError(f"Unknown preset window: {value}")


def build_slot(day_value: str, preset_value: str) -> tuple[datetime, datetime]:
    day = date.fromisoformat(day_value)
    preset = preset_window(preset_value)
    start = datetime.combine(day, preset.start, tzinfo=config.TZ)
    end = datetime.combine(day, preset.end, tzinfo=config.TZ)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def build_slots(day_values: list[str], preset_values: list[str]) -> list[tuple[datetime, datetime]]:
    return [
        build_slot(day_value, preset_value)
        for day_value in day_values
        for preset_value in preset_values
    ]


def combine_day_time(day_value: str, value: time) -> datetime:
    day = date.fromisoformat(day_value)
    return datetime.combine(day, value, tzinfo=config.TZ)


def parse_clock_time(text: str) -> time:
    hour, minute = timeparse.parse_time(text)
    if hour == 24:
        raise timeparse.ParseError("時間不能是 24:00")
    return time(hour, minute)


def build_mode_slot(
    day_value: str,
    mode: str,
    start_text: str = "",
    end_text: str = "",
) -> tuple[datetime, datetime]:
    if mode == "all_day":
        start = combine_day_time(day_value, DEFAULT_DAY_START)
        end = combine_day_time(day_value, DEFAULT_DAY_END)
    elif mode == "after":
        start = combine_day_time(day_value, parse_clock_time(start_text))
        end = combine_day_time(day_value, DEFAULT_DAY_END)
    elif mode == "before":
        start = combine_day_time(day_value, DEFAULT_DAY_START)
        end = combine_day_time(day_value, parse_clock_time(end_text))
    elif mode == "specific":
        start = combine_day_time(day_value, parse_clock_time(start_text))
        end = combine_day_time(day_value, parse_clock_time(end_text))
    else:
        raise ValueError(f"Unknown availability mode: {mode}")
    if end <= start:
        end += timedelta(days=1)
    return start, end


def build_mode_slots(
    day_values: list[str],
    mode: str,
    start_text: str = "",
    end_text: str = "",
) -> list[tuple[datetime, datetime]]:
    return [
        build_mode_slot(day_value, mode, start_text=start_text, end_text=end_text)
        for day_value in day_values
    ]
