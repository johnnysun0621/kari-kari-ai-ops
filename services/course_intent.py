"""課程助教的需求收斂與輕量檢索。"""
import json
import re
from dataclasses import dataclass
from datetime import datetime

import httpx

import config
from services.repositories import CourseMeetingRecord


_OTHER_FOLLOWUP_RE = re.compile(r"其他|另外|別的|其餘|更多|換一堂|換一個|下一堂|再看看")


@dataclass(frozen=True)
class QueryIntent:
    mode: str = "course_question"
    month: str | None = None
    record_ids: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()
    attachment_names: tuple[str, ...] = ()


def _month_from_text(text: str) -> str | None:
    match = re.search(r"(20\d{2})\s*[-年/]\s*(\d{1,2})\s*(?:月)?", text)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}"
    match = re.search(r"(?<!\d)(\d{1,2})\s*月", text)
    if match:
        return f"{datetime.now(config.get_tenant().tz).year}-{int(match.group(1)):02d}"
    return None


def _fallback_intent(question: str, records: list[CourseMeetingRecord]) -> QueryIntent:
    month = _month_from_text(question)
    normalized = question.lower()
    matches = [
        record.page_id for record in records
        if any(term in f"{record.title} {record.summary} {record.content}".lower()
               for term in normalized.split() if len(term) >= 2)
    ]
    if month:
        matches = [record.page_id for record in records
                   if record.date and record.date.strftime("%Y-%m") == month]
        return QueryIntent(mode="month_overview", month=month, record_ids=tuple(matches))
    attachment_names = tuple(
        attachment.name
        for record in records
        for attachment in record.attachments
        if attachment.name.lower() in normalized
    )
    return QueryIntent(record_ids=tuple(matches), attachment_names=attachment_names)


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
) -> QueryIntent:
    """用短上下文判斷意圖；失敗時退回本地月份／文字檢索。"""
    tenant = config.get_tenant()
    if _is_other_followup(question, memory):
        return QueryIntent(mode="compare")
    if not tenant.llm_api_key or not records:
        return _fallback_intent(question, records)
    previous = json.dumps(memory or {}, ensure_ascii=False)[:1200]
    payload = {
        "model": tenant.llm_model,
        "temperature": 0,
        "max_tokens": 220,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是課程資料檢索路由器。只依據課程目錄選出相關 page_id，"
                    "不要回答問題。回傳 JSON：mode、month、record_ids、terms、attachment_names。"
                    "mode 可用 month_overview、course_question、transcript_search、"
                    "task_lookup、compare。record_ids 和 attachment_names 必須來自目錄；找不到就空陣列。"
                ),
            },
            {
                "role": "user",
                "content": f"上一輪摘要：{previous}\n問題：{question}\n課程目錄：\n{_catalog(records)}",
            },
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{tenant.llm_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {tenant.llm_api_key}"},
                json=payload,
            )
            response.raise_for_status()
        raw = response.json()["choices"][0]["message"]["content"]
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
        return QueryIntent(
            mode=str(data.get("mode", "course_question")),
            month=data.get("month") or _month_from_text(question),
            record_ids=record_ids,
            terms=tuple(str(value) for value in data.get("terms", [])[:8]),
            attachment_names=attachment_names,
        )
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
        return _fallback_intent(question, records)


def select_records(
    records: list[CourseMeetingRecord], intent: QueryIntent, memory: dict | None = None
) -> list[CourseMeetingRecord]:
    by_id = {record.page_id: record for record in records}
    selected = [by_id[page_id] for page_id in intent.record_ids if page_id in by_id]
    if selected:
        return selected
    if intent.month:
        monthly = [record for record in records
                   if record.date and record.date.strftime("%Y-%m") == intent.month]
        if monthly:
            return monthly
    if intent.terms:
        terms = [term.lower() for term in intent.terms if len(term) >= 2]
        matched = [
            record for record in records
            if any(term in f"{record.title} {record.summary} {record.content}".lower() for term in terms)
        ]
        if matched:
            return matched
    previous_ids = (memory or {}).get("record_ids", [])
    if intent.mode == "compare" and previous_ids:
        remaining = [record for record in records if record.page_id not in previous_ids]
        if remaining:
            return remaining[:8]
    previous = [by_id[page_id] for page_id in previous_ids if page_id in by_id]
    return previous or records[:8]
