"""課程 AI 助教：以已授權的 Notion 課程紀錄回答問題。"""
import httpx

import config
from services.repositories import CourseMeetingRecord


class CourseQAError(Exception):
    pass


def _record_text(record: CourseMeetingRecord) -> str:
    date_text = record.date.strftime("%Y-%m-%d") if record.date else "未填日期"
    body = record.content or record.summary or "（沒有可讀取的內容）"
    return f"【{record.title}｜{date_text}】\n{body[:6000]}\n來源：{record.url}"


async def answer_course_question(
    artist_name: str,
    question: str,
    records: list[CourseMeetingRecord],
    intent_context: str = "",
) -> str:
    tenant = config.get_tenant()
    if not tenant.llm_api_key:
        raise CourseQAError("尚未設定 LLM_API_KEY")
    context = "\n\n".join(_record_text(record) for record in records)[:24000]
    payload = {
        "model": tenant.llm_model,
        "temperature": 0.2,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是課程助教，只能根據提供的 Notion 課程會議紀錄回答。"
                    "不可猜測或補充資料；找不到答案時明確說明紀錄不足。"
                    "回答使用繁體中文，簡潔、分點，並保留來源頁面名稱。"
                    "若問題是總覽，請整理重點；若是逐字稿搜尋，請指出提到它的課程與段落。"
                ),
            },
            {
                "role": "user",
                "content": f"藝人：{artist_name}\n檢索意圖：{intent_context}\n問題：{question}\n\n可用課程紀錄：\n{context}",
            },
        ],
    }
    async with httpx.AsyncClient(timeout=45) as client:
        try:
            response = await client.post(
                f"{tenant.llm_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {tenant.llm_api_key}"},
                json=payload,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise CourseQAError("LLM API 呼叫失敗") from exc
    data = response.json()
    try:
        answer = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise CourseQAError("LLM API 回傳格式不正確") from exc
    sources = "\n".join(f"・[{record.title}]({record.url})" for record in records if record.url)
    return f"{answer}\n\n**參考課程紀錄**\n{sources}"[:3900]
