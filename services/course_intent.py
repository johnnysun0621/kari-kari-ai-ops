"""課程助教的需求收斂與輕量檢索。"""
import json
import re
from dataclasses import dataclass
from datetime import datetime

import httpx

import config
from services import llm_response, llm_usage
from services.repositories import CourseMeetingRecord


_OTHER_FOLLOWUP_RE = re.compile(r"其他|另外|別的|其餘|更多|換一堂|換一個|下一堂|再看看")
_SUMMARY_QUERY_RE = re.compile(r"整理.*(?:筆記|重點|摘要)|(?:筆記|重點|摘要).*整理|幫我整理|複習")
_BROAD_QUERY_RE = re.compile(
    r"上過|上了|學過|有哪些|哪幾堂|什麼課|啥課|全部|所有|總覽|統整|整理|課程紀錄|上課紀錄"
)
_EXPLICIT_DATE_RE = re.compile(
    r"(20\d{2})\s*[-年/]\s*(\d{1,2})(?:\s*月)?(?:\s*[-/日]\s*\d{1,2})?"
)
_EXPLICIT_MONTH_RE = re.compile(r"(?<![\d/-])(\d{1,2})\s*月")
_CHINESE_MONTHS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
    "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
}
MAX_SELECTED_RECORDS = 8


@dataclass(frozen=True)
class QueryIntent:
    mode: str = "course_question"
    month: str | None = None
    record_ids: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()
    attachment_names: tuple[str, ...] = ()
    broad: bool = False


def _month_from_text(text: str) -> str | None:
    match = re.search(r"(20\d{2})\s*[-年/]\s*(\d{1,2})\s*(?:月)?", text)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}"
    match = re.search(r"(?<!\d)(\d{1,2})\s*月", text)
    if match:
        return f"{datetime.now(config.get_tenant().tz).year}-{int(match.group(1)):02d}"
    match = re.search(r"(十一|十二|一|二|三|四|五|六|七|八|九|十)\s*月", text)
    if match:
        return f"{datetime.now(config.get_tenant().tz).year}-{_CHINESE_MONTHS[match.group(1)]:02d}"
    return None


def record_months(record: CourseMeetingRecord) -> set[str]:
    """Return months explicitly labelled by the course record before its Date fallback.

    KariKari's Date property may describe record creation or recording time, while
    the page title/summary can contain the actual lesson date (for example,
    ``測試用｜2026-08-03``).  Prefer those explicit labels for month queries.
    """
    months: set[str] = set()
    for text in (record.title, record.summary):
        for match in _EXPLICIT_DATE_RE.finditer(text):
            year_text, month_text = match.groups()
            month = int(month_text)
            if not 1 <= month <= 12:
                continue
            months.add(f"{int(year_text):04d}-{month:02d}")
        for match in _EXPLICIT_MONTH_RE.finditer(text):
            month = int(match.group(1))
            if 1 <= month <= 12:
                year = datetime.now(config.get_tenant().tz).year
                months.add(f"{year:04d}-{month:02d}")
    if months:
        return months
    if record.date:
        return {record.date.strftime("%Y-%m")}
    return set()


def _fallback_intent(question: str, records: list[CourseMeetingRecord]) -> QueryIntent:
    month = _month_from_text(question)
    normalized = question.lower()
    matches = [
        record.page_id for record in records
        if any(term in f"{record.title} {record.summary} {record.content}".lower()
               for term in normalized.split() if len(term) >= 2)
    ]
    if month:
        matches = [record.page_id for record in records if month in record_months(record)]
        return QueryIntent(
            mode="month_overview", month=month, record_ids=tuple(matches), broad=True
        )
    attachment_names = tuple(
        attachment.name
        for record in records
        for attachment in record.attachments
        if attachment.name.lower() in normalized
    )
    return QueryIntent(
        mode="summarize" if _SUMMARY_QUERY_RE.search(question) else "course_question",
        record_ids=tuple(matches),
        attachment_names=attachment_names,
        # 課程助教接受開放式問法。找不到明確關鍵字時，仍提供有限筆
        # 本人可見紀錄給回答模型判斷是否有足夠證據，而不是直接拒答。
        broad=bool(question.strip()),
    )


def is_broad_query(question: str) -> bool:
    """只允許明確的總覽問題擴大到課程目錄，避免模糊問題誤讀資料。"""
    return bool(_BROAD_QUERY_RE.search(question))


def _is_other_followup(question: str, memory: dict | None) -> bool:
    """辨識「再看看其他的」這類延續問題，避免路由器重選上一堂。"""
    return bool(memory and memory.get("record_ids") and _OTHER_FOLLOWUP_RE.search(question))


def _catalog(records: list[CourseMeetingRecord]) -> str:
    lines = []
    for record in records:
        date_text = record.date.strftime("%Y-%m-%d") if record.date else "未填日期"
        summary = (record.summary or "").replace("\n", " ")[:180]
        snippet = (record.content or "").replace("\n", " ")[:240]
        attachments = ", ".join(item.name for item in record.attachments) or "無附件"
        lines.append(f"{record.page_id} | {date_text} | {record.title} | 附件：{attachments} | {summary} | {snippet}")
    return "\n".join(lines)


def _parse_json(text: str) -> dict:
    text = text.strip().replace("```json", "").replace("```", "").strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(match.group(0) if match else text)


async def classify_query(
    question: str,
    records: list[CourseMeetingRecord],
    memory: dict | None = None,
    usage_key: str | None = None,
) -> QueryIntent:
    """用短上下文判斷意圖；失敗時退回本地月份／文字檢索。"""
    tenant = config.get_tenant()
    if _is_other_followup(question, memory):
        return QueryIntent(mode="compare")
    if memory and memory.get("record_ids") and _SUMMARY_QUERY_RE.search(question):
        return QueryIntent(
            mode="summarize",
            month=memory.get("month"),
            record_ids=tuple(str(value) for value in memory.get("record_ids", [])),
            broad=True,
        )
    if _month_from_text(question) and is_broad_query(question):
        return _fallback_intent(question, records)
    if not tenant.llm_api_key or not records or not llm_usage.within_daily_limit(usage_key):
        return _fallback_intent(question, records)
    previous = json.dumps(memory or {}, ensure_ascii=False)[:1200]
    payload = {
        "model": tenant.llm_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是開放式課程資料檢索路由器。使用者可以用任何自然語句提問。"
                    "先判斷任務，再只依據課程目錄選出需要的 page_id，不要直接回答問題。"
                    "回傳 JSON：mode、month、record_ids、terms、attachment_names、broad。"
                    "mode 可用 month_overview、summarize、explain、transcript_search、"
                    "task_lookup、compare、action_plan、practice、course_question。"
                    "明確指定月份、課程、老師、關鍵字或附件時縮小範圍；"
                    "延續問題優先沿用上一輪；需要跨課整理時 broad=true。"
                    "record_ids 和 attachment_names 必須來自目錄。資料可能不足時仍選最相關紀錄，"
                    "讓回答階段根據證據說明不足，不要因為問法不在範例中就拒絕。"
                ),
            },
            {
                "role": "user",
                "content": f"上一輪摘要：{previous}\n問題：{question}\n課程目錄：\n{_catalog(records)}",
            },
        ],
    }
    llm_response.configure_chat_completion(
        payload,
        base_url=tenant.llm_base_url,
        model=tenant.llm_model,
        max_tokens=220,
    )
    if "longcat" in tenant.llm_base_url.lower():
        payload["temperature"] = 0
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{tenant.llm_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {tenant.llm_api_key}"},
                json=payload,
            )
            response.raise_for_status()
        response_data = response.json()
        raw = llm_response.extract_text(response_data)
        if not raw:
            raise ValueError("LLM intent response has no text")
        llm_usage.record(response_data.get("usage"), user_key=usage_key)
        data = _parse_json(raw)
        valid_ids = {record.page_id for record in records}
        valid_attachment_names = {
            attachment.name for record in records for attachment in record.attachments
        }
        record_ids = tuple(str(value) for value in data.get("record_ids", []) if str(value) in valid_ids)
        attachment_names = tuple(
            str(value) for value in data.get("attachment_names", [])
            if str(value) in valid_attachment_names
        )
        mode = str(data.get("mode", "course_question"))
        if mode not in {
            "month_overview", "summarize", "explain", "transcript_search",
            "task_lookup", "compare", "action_plan", "practice", "course_question",
        }:
            mode = "course_question"
        month = data.get("month") or _month_from_text(question)
        if month and not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", str(month)):
            month = _month_from_text(question)
        broad = bool(data.get("broad")) or is_broad_query(question) or not record_ids
        return QueryIntent(
            mode=mode,
            month=month,
            record_ids=record_ids,
            terms=tuple(str(value) for value in data.get("terms", [])[:8]),
            attachment_names=attachment_names,
            broad=broad,
        )
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
        return _fallback_intent(question, records)


def select_records(
    records: list[CourseMeetingRecord], intent: QueryIntent, memory: dict | None = None
) -> list[CourseMeetingRecord]:
    by_id = {record.page_id: record for record in records}
    selected = [by_id[page_id] for page_id in intent.record_ids if page_id in by_id]
    if selected:
        return selected[:MAX_SELECTED_RECORDS]
    if intent.month:
        monthly = [record for record in records if intent.month in record_months(record)]
        if monthly:
            return monthly[:MAX_SELECTED_RECORDS]
    if intent.terms:
        terms = [term.lower() for term in intent.terms if len(term) >= 2]
        matched = [
            record for record in records
            if any(term in f"{record.title} {record.summary} {record.content}".lower() for term in terms)
        ]
        if matched:
            return matched[:MAX_SELECTED_RECORDS]
    previous_ids = (memory or {}).get("record_ids", [])
    if intent.mode == "compare" and previous_ids:
        remaining = [record for record in records if record.page_id not in previous_ids]
        if remaining:
            return remaining[:MAX_SELECTED_RECORDS]
    previous = [by_id[page_id] for page_id in previous_ids if page_id in by_id]
    if previous:
        return previous[:MAX_SELECTED_RECORDS]
    return records[:MAX_SELECTED_RECORDS] if intent.broad else []
