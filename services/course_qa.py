"""課程 AI 助教：以已授權的 Notion 課程紀錄回答問題。"""
import httpx

import config
from services import llm_usage
from services.repositories import CourseMeetingRecord


class CourseQAError(Exception):
    def __init__(self, message: str, user_message: str | None = None):
        super().__init__(message)
        self.user_message = user_message or "⚠️ 課程助教目前無法完成回答，請稍後再試。"


def _record_text(record: CourseMeetingRecord, attachment_names: tuple[str, ...] = ()) -> str:
    date_text = record.date.strftime("%Y-%m-%d") if record.date else "未填日期"
    body = record.content or record.summary or "（沒有可讀取的內容）"
    attachment_text = "\n\n".join(
        f"【附件：{attachment.name}】\n{attachment.text[:3500]}"
        for attachment in record.attachments
        if attachment.name in attachment_names and attachment.text
    )
    sections = [f"【{record.title}｜{date_text}】\n{body[:3500]}"]
    if attachment_text:
        sections.append(attachment_text)
    sections.append(f"來源：{record.url}")
    return "\n\n".join(sections)


async def answer_course_question(
    artist_name: str,
    question: str,
    records: list[CourseMeetingRecord],
    intent_context: str = "",
    attachment_names: tuple[str, ...] = (),
) -> str:
    tenant = config.get_tenant()
    if not tenant.llm_api_key:
        raise CourseQAError(
            "尚未設定 LLM_API_KEY",
            "⚠️ 課程助教尚未設定 API，請聯絡控方。",
        )
    if not llm_usage.within_daily_limit():
        raise CourseQAError(
            "LLM daily token limit reached",
            "⚠️ 課程助教今日使用量已達上限，請明天再試或聯絡控方。",
        )
    context = "\n\n".join(_record_text(record, attachment_names) for record in records)[:16000]
    payload = {
        "model": tenant.llm_model,
        "temperature": 0.2,
        "max_tokens": tenant.llm_max_tokens,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是課程助教，只能根據提供的 Notion 課程會議紀錄回答。"
                    "不可猜測或補充資料；找不到答案時明確說明紀錄不足。"
                    "回答使用繁體中文，簡潔、分點，並保留來源頁面名稱。"
                    "附件與頁面正文是分開的；只有使用者指定的附件才可引用附件內容。"
                    "若問題是總覽，請整理重點；若是逐字稿搜尋，請指出提到它的課程與段落。"
                ),
            },
            {
                "role": "user",
                "content": f"藝人：{artist_name}\n檢索意圖：{intent_context}\n問題：{question}\n\n可用課程紀錄：\n{context}",
            },
        ],
    }
    async with httpx.AsyncClient(timeout=60) as client:
        try:
            response = await client.post(
                f"{tenant.llm_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {tenant.llm_api_key}"},
                json=payload,
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise CourseQAError(
                "LLM API 回應逾時",
                "⚠️ 課程助教回應逾時，請稍後再試。",
            ) from exc
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:300].replace("\n", " ")
            raise CourseQAError(
                f"LLM API HTTP {exc.response.status_code}: {body}",
                "⚠️ 課程助教 API 暫時無法回應，請稍後再試。",
            ) from exc
        except httpx.HTTPError as exc:
            raise CourseQAError(
                "LLM API 網路呼叫失敗",
                "⚠️ 課程助教暫時連不上 API，請稍後再試。",
            ) from exc
    data = response.json()
    llm_usage.record(data.get("usage"))
    try:
        answer = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise CourseQAError("LLM API 回傳格式不正確") from exc
    sources = "\n".join(f"・[{record.title}]({record.url})" for record in records if record.url)
    return f"{answer}\n\n**參考課程紀錄**\n{sources}"[:3900]
