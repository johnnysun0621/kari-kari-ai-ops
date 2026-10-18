"""小律的自然語言查任務（M6-03，2026-10-18）。

09/22 會議：「藝人可在 DC 上透過會議秘書以自然語言查詢屬於自己的任務（輸入日期即可）」。
規則（2026-10-18 定）：
- 只查自己的任務，範圍與 /我的任務 一致（Discord 顯示有勾、未結案）
- 只查詢不操作：交件、確認收到仍走 /我的任務
- 不經過 LLM：判斷與篩選都在這裡用規則做，任務資料不送進模型，也不會被模型改寫或帶出網址

這裡全是純函式；讀 Notion 與組 Discord 訊息在 cogs/course_qa.py。
"""
import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from services import repositories

# 問的是「要做／要交什麼、什麼時候」→ 任務；問的是「會議／課上講了什麼」→ 會議紀錄。
_TASK_RE = re.compile(
    r"任務|待辦|要交|交件|還沒交|沒交|未交|要做|截止|期限|deadline|ddl|due|行程|通告|演出|檔期"
    r"|作業.{0,6}(?:交|截止|期限)|(?:什麼|哪些|有啥).{0,4}(?:要忙|工作|事要做|事情)",
    re.IGNORECASE,
)
_MEETING_RE = re.compile(r"會議|開會|開過會|討論|逐字稿|筆記|重點|摘要|上課.{0,4}(?:教|講|說)|課程紀錄|上課紀錄|教了")
_PENDING_RE = re.compile(r"還沒交|沒交|未交|要交|待交|該交")

MAX_LINES = 20
MAX_DETAIL_CHARS = 1500
MIN_NAME_MATCH = 4

# 想看某一筆任務的內容（2026-10-18 QA：藝人問任務詳細內容，小律只會列清單）
_DETAIL_RE = re.compile(r"詳細|詳情|內容|說明|要求|細節|介紹|在講什麼|是什麼|做什麼|看一下|看看")
_INDEX_RE = re.compile(r"第\s*([0-9０-９]+|[一二三四五六七八九十]+)\s*(?:個|筆|項|件|條)?")
_CHINESE_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_NORMALIZE_RE = re.compile(r"[\s　（）()【】\[\]｜|,，、。:：/／&＆_＿\-－~～]+")


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date
    label: str


def is_task_query(question: str) -> bool:
    """有任務字眼、而且不是在問會議內容。兩邊都有時（例如「會議提到的任務」）交給會議紀錄。"""
    return bool(_TASK_RE.search(question)) and not _MEETING_RE.search(question)


def wants_pending_only(question: str) -> bool:
    return bool(_PENDING_RE.search(question))


def _week(today: date, offset_weeks: int) -> tuple[date, date]:
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset_weeks)
    return monday, monday + timedelta(days=6)


def _month(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def parse_range(question: str, now: datetime) -> DateRange | None:
    """把「今天、明天、這週、下週、這個月、11 月、10/25、最近」轉成日期區間；沒提到日期回 None。"""
    today = now.date()
    if "後天" in question:
        day = today + timedelta(days=2)
        return DateRange(day, day, f"後天（{day.month}/{day.day}）")
    if "明天" in question:
        day = today + timedelta(days=1)
        return DateRange(day, day, f"明天（{day.month}/{day.day}）")
    if "今天" in question or "今日" in question:
        return DateRange(today, today, f"今天（{today.month}/{today.day}）")
    if re.search(r"下(?:週|周|禮拜|星期)", question):
        start, end = _week(today, 1)
        return DateRange(start, end, f"下週（{start.month}/{start.day}～{end.month}/{end.day}）")
    if re.search(r"(?:這|本)(?:週|周|禮拜|星期)", question):
        start, end = _week(today, 0)
        return DateRange(start, end, f"這週（{start.month}/{start.day}～{end.month}/{end.day}）")
    if re.search(r"下(?:個)?月", question):
        year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        start, end = _month(year, month)
        return DateRange(start, end, f"{month} 月")
    if re.search(r"(?:這|本)(?:個)?月", question):
        start, end = _month(today.year, today.month)
        return DateRange(start, end, f"{today.month} 月")
    day_match = re.search(r"(?<!\d)(\d{1,2})\s*[/月]\s*(\d{1,2})(?:\s*[日號])?", question)
    if day_match:
        month, day_num = int(day_match.group(1)), int(day_match.group(2))
        year = today.year if month >= today.month - 1 else today.year + 1
        try:
            day = date(year, month, day_num)
        except ValueError:
            day = None
        if day:
            return DateRange(day, day, f"{day.month}/{day.day}")
    month_match = re.search(r"(?<!\d)(\d{1,2})\s*月", question)
    if month_match and 1 <= int(month_match.group(1)) <= 12:
        month = int(month_match.group(1))
        year = today.year if month >= today.month - 1 else today.year + 1
        start, end = _month(year, month)
        return DateRange(start, end, f"{month} 月")
    if re.search(r"最近|接下來|近期|這幾天", question):
        return DateRange(today, today + timedelta(days=13), "接下來兩週")
    return None


def _task_dates(task: repositories.TaskItem) -> list[date]:
    deadline, _ = repositories.task_action_deadline(task)
    candidates = [task.start, deadline, task.end, task.revision_deadline]
    return [value.date() for value in candidates if value is not None]


def select_tasks(
    tasks: list[repositories.TaskItem],
    date_range: DateRange | None,
    *,
    pending_only: bool = False,
) -> list[repositories.TaskItem]:
    """未結案、（有區間時）開始日或期限落在區間內；依期限排序，沒期限排最後。"""
    picked = []
    for task in tasks:
        if task.status in repositories.INACTIVE_TASK_STATUSES:
            continue
        if pending_only and not repositories.is_task_submittable(task):
            continue
        if date_range is not None and not any(
            date_range.start <= day <= date_range.end for day in _task_dates(task)
        ):
            continue
        picked.append(task)

    def sort_key(task: repositories.TaskItem):
        deadline, _ = repositories.task_action_deadline(task)
        return (deadline is None, deadline.timestamp() if deadline else 0, task.name)

    return sorted(picked, key=sort_key)


def wants_detail(question: str) -> bool:
    return bool(_DETAIL_RE.search(question))


def index_reference(question: str) -> int | None:
    """「第 2 個」「第三筆」→ 2、3（指上一次列出的清單）。"""
    match = _INDEX_RE.search(question)
    if not match:
        return None
    raw = match.group(1).translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    if raw.isdigit():
        return int(raw)
    if raw in _CHINESE_DIGITS:
        return _CHINESE_DIGITS[raw]
    if raw.startswith("十") and raw[1:] in _CHINESE_DIGITS:
        return 10 + _CHINESE_DIGITS[raw[1:]]
    return None


def _normalize(text: str) -> str:
    # 數字也拿掉：任務名稱常以日期開頭（「10/25（日）week4…」），問「10/25 要交什麼」不該被當成點名那筆。
    return re.sub(r"[0-9０-９]+", "", _NORMALIZE_RE.sub("", text)).lower()


def _longest_common_run(a: str, b: str) -> int:
    best = 0
    previous = [0] * (len(b) + 1)
    for char_a in a:
        current = [0] * (len(b) + 1)
        for j, char_b in enumerate(b, start=1):
            if char_a == char_b:
                current[j] = previous[j - 1] + 1
                best = max(best, current[j])
        previous = current
    return best


def match_tasks_by_name(question: str, tasks: list[repositories.TaskItem]) -> list[repositories.TaskItem]:
    """問題裡提到的任務：名稱連續重疊最長的那幾筆（至少 4 個字，避免「任務」「作業」這種泛詞誤中）。"""
    normalized_question = _normalize(question)
    scored = []
    for task in tasks:
        if task.status in repositories.INACTIVE_TASK_STATUSES:
            continue
        name = _normalize(task.name)
        if not name:
            continue
        score = len(name) if name in normalized_question else _longest_common_run(name, normalized_question)
        if score >= min(MIN_NAME_MATCH, len(name)):
            scored.append((score, task))
    if not scored:
        return []
    best = max(score for score, _ in scored)
    return [task for score, task in scored if score == best]


def is_meeting_question(question: str) -> bool:
    return bool(_MEETING_RE.search(question))
