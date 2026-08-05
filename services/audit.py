"""Audit Log 寫入。失敗只記 log，不阻斷主流程。"""
import logging
from datetime import datetime

import config
from services import notion_api
from services.repositories import _date, _rich_text, _select, _title, _url

logger = logging.getLogger(__name__)


async def log_event(
    event_name: str,
    action_type: str,  # 查詢/建立/更新/提交/上傳/提醒/同步/錯誤
    operator: str,
    discord_user_id: str = "",
    target_db: str = "",
    target_url: str = "",
    before: str = "",
    after: str = "",
    result: str = "成功",  # 成功/失敗/部分成功
    error_message: str = "",
    source: str = "Discord",
) -> None:
    props = {
        "事件名稱": _title(event_name),
        "操作時間": _date(datetime.now(config.TZ)),
        "操作類型": _select(action_type),
        "操作者": _rich_text(operator),
        "來源系統": _select(source),
        "結果": _select(result),
    }
    if discord_user_id:
        props["Discord User ID"] = _rich_text(str(discord_user_id))
    if target_db:
        props["目標資料庫"] = _rich_text(target_db)
    if target_url:
        props["目標頁面 URL"] = _url(target_url)
    if before:
        props["前值"] = _rich_text(before)
    if after:
        props["後值"] = _rich_text(after)
    if error_message:
        props["錯誤訊息"] = _rich_text(error_message)
    try:
        await notion_api.create_page(config.DS_AUDIT_LOG, props)
    except Exception:
        logger.exception("Audit Log 寫入失敗（不阻斷主流程）: %s", event_name)


async def has_successful_event(event_name: str, target_url: str) -> bool:
    """跨重啟檢查重要操作是否已完成；查詢失敗交由呼叫端決定是否中止。"""
    if not target_url:
        return False
    pages = await notion_api.query_data_source(
        config.DS_AUDIT_LOG,
        filter_={"and": [
            {"property": "事件名稱", "title": {"equals": event_name}},
            {"property": "目標頁面 URL", "url": {"equals": target_url}},
            {"property": "結果", "select": {"equals": "成功"}},
        ]},
    )
    return bool(pages)
