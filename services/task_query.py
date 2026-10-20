"""小律的自然語言查任務（M6-03，2026-10-18）。

09/22 會議：「藝人可在 DC 上透過會議秘書以自然語言查詢屬於自己的任務（輸入日期即可）」。
規則（2026-10-18 定）：
- 只查自己的任務，範圍與 /我的任務 一致（Discord 顯示有勾、未結案）
- 只查詢不操作：交件、確認收到仍走 /我的任務
- 清單與「某筆的完整內容」不經 LLM：判斷與篩選都在這裡用規則做
- 具體問題（幾題、什麼格式、要準備什麼）才交給模型，而且只送這位藝人被點名／區間內的任務，
  欄位見 build_task_context()，規則見 TASK_ASSISTANT_SKILL.md（2026-10-18 依正式任務頁實撈結構撰寫）

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


# ---------- 任務問答（LLM）用的資料整理 ----------

# 時間池自動產生的任務草稿會在「任務說明」留下控方內部字樣（2026-10-18 實撈：10 筆），
# 給藝人看之前拿掉；藝人回報追加在說明裡的舊格式紀錄也一併拿掉。
_INTERNAL_PREFIXES = (
    "安排來源：", "安排類型：", "任務類型：", "此為控方草稿", "此 Task 同時管理",
    "候選時段（由營運最後確認）", "需要時長：",
)
_CANDIDATE_SLOT_RE = re.compile(r"^\s*20\d{2}-\d{2}-\d{2}\s+\d{1,2}:\d{2}\s*[~～-]\s*\d{1,2}:\d{2}\s*$")
_SPECIFIC_RE = re.compile(
    r"幾|多少|怎麼|如何|格式|哪裡|在哪|為什麼|要不要|可不可以|可以.{0,6}嗎|需要|準備|誰|何時|什麼時候|哪天|哪一天|用什麼|交什麼形式"
)
MAX_CONTEXT_TASKS = 8


def clean_description(text: str) -> str:
    lines: list[str] = []
    skipping_feedback = False
    in_candidates = False
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("【Discord 任務回饋"):
            skipping_feedback = True
            continue
        if skipping_feedback:
            if not stripped:
                skipping_feedback = False
            continue
        if stripped.startswith("候選時段（由營運最後確認）"):
            in_candidates = True
            continue
        if in_candidates and _CANDIDATE_SLOT_RE.match(stripped):
            continue
        in_candidates = False
        if stripped.startswith(_INTERNAL_PREFIXES):
            continue
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def is_specific_question(question: str) -> bool:
    """問的是任務裡的某個細節（幾題、什麼格式、要準備什麼），不是單純要清單或全文。"""
    return bool(_SPECIFIC_RE.search(question))


def build_task_context(
    task: repositories.TaskItem,
    *,
    now: datetime,
    deadline_text: str,
    project_name: str | None = None,
    teacher_names: list[str] = (),
    reviewer_names: list[str] = (),
    body: str = "",
) -> str:
    """一筆任務給模型看的文字。欄位對應 TASK_ASSISTANT_SKILL.md 的說明；不放任何網址或 page id。"""
    from services.notion_links import strip_notion_links

    lines = [f"任務名稱：{strip_notion_links(task.name)}"]
    if task.task_type:
        lines.append(f"任務類型：{task.task_type}")
    if project_name:
        lines.append(f"所屬專案：{project_name}")
    lines.append(f"狀態：{task.status or '未填'}")
    if task.start:
        start = f"{task.start:%Y-%m-%d}" + (f" {task.start:%H:%M}" if task.start_has_time else "")
        lines.append(f"開始：{start}")
    lines.append(f"期限：{deadline_text}（今天是 {now:%Y-%m-%d}）")
    if repositories.is_task_submittable(task):
        lines.append("可以交件：是（用 /我的任務 → 提交任務，上傳到公司 Drive）")
    if task.course_category:
        lines.append(f"課程類別：{task.course_category}")
    if teacher_names:
        lines.append(f"負責老師：{'、'.join(teacher_names)}")
    if reviewer_names:
        lines.append(f"審核人員：{'、'.join(reviewer_names)}")
    content = clean_description(strip_notion_links(body))
    lines.append(f"任務內容：\n{content}" if content else "任務內容：（控方沒有寫）")
    return "\n".join(lines)


def task_detail_text(task: repositories.TaskItem, *, deadline_text: str, body: str) -> str:
    """任務詳細（基本資訊＋任務內容），小律與每日早報的「看某一筆的詳細」共用。"""
    from services.notion_links import strip_notion_links

    content = clean_description(strip_notion_links(body or ""))
    if len(content) > MAX_DETAIL_CHARS:
        content = content[:MAX_DETAIL_CHARS].rstrip() + "…（內容較長，只顯示前段；完整內容請問控方）"
    info = [
        f"・類型：{task.task_type or '任務'}",
        f"・狀態：{task.status or '未填狀態'}",
        f"・期限：{deadline_text}",
    ]
    if task.start:
        start_text = f"{task.start.month}/{task.start.day}"
        if task.start_has_time:
            start_text += task.start.strftime(" %H:%M")
        info.insert(2, f"・開始：{start_text}")
    if repositories.is_task_submittable(task):
        info.append("・可以交件：用 `/我的任務` → 提交任務")
    return (
        f"📌 **{strip_notion_links(task.name)}**\n"
        + "\n".join(info)
        + "\n\n**任務內容**\n"
        + (content or "（控方還沒寫任務內容）")
    )
