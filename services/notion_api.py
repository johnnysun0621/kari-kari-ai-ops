"""Notion REST API 薄封裝（API 版本 2025-09-03，data source 模型）。

只做三件事：query data source、create page、update page。
含 429 / 5xx 重試與統一錯誤型別，欄位組裝交給 repositories.py。
"""
import asyncio
import logging

import httpx

import config

logger = logging.getLogger(__name__)

BASE_URL = "https://api.notion.com/v1"
MAX_RETRIES = 3


class NotionAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"Notion API {status_code}: {message}")


_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {config.NOTION_TOKEN}",
                "Notion-Version": config.NOTION_VERSION,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()


async def _request(method: str, path: str, json_body: dict | None = None) -> dict:
    client = _get_client()
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.request(method, path, json=json_body)
        except httpx.HTTPError as e:
            last_error = e
            await asyncio.sleep(1.5 * (attempt + 1))
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = float(resp.headers.get("Retry-After", 1.5 * (attempt + 1)))
            logger.warning("Notion %s %s -> %s，%.1fs 後重試", method, path, resp.status_code, retry_after)
            await asyncio.sleep(retry_after)
            last_error = NotionAPIError(resp.status_code, resp.text[:500])
            continue
        if resp.status_code >= 400:
            raise NotionAPIError(resp.status_code, resp.text[:500])
        return resp.json()
    raise last_error if last_error else NotionAPIError(0, "unknown error")


async def query_data_source(
    data_source_id: str,
    filter_: dict | None = None,
    sorts: list[dict] | None = None,
) -> list[dict]:
    """查詢 data source，自動翻頁，回傳所有 page 物件。"""
    results: list[dict] = []
    body: dict = {}
    if filter_:
        body["filter"] = filter_
    if sorts:
        body["sorts"] = sorts
    cursor: str | None = None
    while True:
        if cursor:
            body["start_cursor"] = cursor
        data = await _request("POST", f"/data_sources/{data_source_id}/query", body)
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return results


async def create_page(data_source_id: str, properties: dict) -> dict:
    body = {
        "parent": {"type": "data_source_id", "data_source_id": data_source_id},
        "properties": properties,
    }
    return await _request("POST", "/pages", body)


async def update_page(page_id: str, properties: dict | None = None, archived: bool | None = None) -> dict:
    body: dict = {}
    if properties is not None:
        body["properties"] = properties
    if archived is not None:
        body["archived"] = archived
    return await _request("PATCH", f"/pages/{page_id}", body)
