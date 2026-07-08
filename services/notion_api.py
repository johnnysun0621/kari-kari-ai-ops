"""Notion REST API 薄封裝（API 版本 2025-09-03，data source 模型）。

只做四件事：query data source、retrieve page、create page、update page。
含 429 / 5xx 重試、節流與統一錯誤型別，欄位組裝交給 repositories.py。

連線與節流以 integration token 為 key（每租戶一份）：Notion 限流是
per-integration（~3 req/s），多租戶時 A 公司的請求不會卡在 B 公司後面。
"""
import asyncio
import logging
import time

import httpx

import config

logger = logging.getLogger(__name__)

BASE_URL = "https://api.notion.com/v1"
MAX_RETRIES = 3
# Notion 官方限流約 3 requests/second（per token）。用最小間隔「主動」節流，
# 避免多個互動同時爆發請求觸發 429 → 連鎖重試 sleep → 互動轉圈失敗。
_MIN_REQUEST_INTERVAL = 0.34  # 秒


class NotionAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"Notion API {status_code}: {message}")


class _TokenState:
    """單一 integration token 的 HTTP 連線與節流狀態。"""

    def __init__(self, token: str, notion_version: str):
        self.client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": notion_version,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        self.rate_lock = asyncio.Lock()
        self.last_request_ts = 0.0

    async def throttle(self) -> None:
        """確保同一 token 的請求送出間隔 >= _MIN_REQUEST_INTERVAL（跨互動共用）。"""
        async with self.rate_lock:
            wait = _MIN_REQUEST_INTERVAL - (time.monotonic() - self.last_request_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            self.last_request_ts = time.monotonic()


_states: dict[str, _TokenState] = {}


def _get_state(token: str | None = None) -> _TokenState:
    tenant = config.get_tenant()
    key = token or tenant.notion_token
    state = _states.get(key)
    if state is None or state.client.is_closed:
        state = _TokenState(key, tenant.notion_version)
        _states[key] = state
    return state


async def close_client() -> None:
    for state in _states.values():
        if not state.client.is_closed:
            await state.client.aclose()
    _states.clear()


async def _request(method: str, path: str, json_body: dict | None = None) -> dict:
    state = _get_state()
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        await state.throttle()
        try:
            resp = await state.client.request(method, path, json=json_body)
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


async def retrieve_page(page_id: str) -> dict:
    """讀取單一 page（GET /pages/{id}），取代「撈全表再線性搜尋」。"""
    return await _request("GET", f"/pages/{page_id}")


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
