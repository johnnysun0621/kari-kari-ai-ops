"""LLM 使用量記錄與每日護欄；只保存 token 數，不保存問題或課程內容。"""
import json
from datetime import datetime
from pathlib import Path

import config

STATE_PATH = Path(".state/llm_usage.json")


def _today() -> str:
    return datetime.now(config.get_tenant().tz).date().isoformat()


def _load() -> dict:
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def _save(data: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def daily_total() -> int:
    data = _load()
    return int(data.get(config.get_tenant().name, {}).get(_today(), 0))


def within_daily_limit() -> bool:
    limit = config.get_tenant().llm_daily_token_limit
    return limit <= 0 or daily_total() < limit


def record(usage: dict | None) -> int:
    """記錄 API 回傳的 total_tokens；回傳今日累計。"""
    if not usage:
        return daily_total()
    tokens = usage.get("total_tokens")
    if not isinstance(tokens, (int, float)):
        return daily_total()
    data = _load()
    tenant_data = data.setdefault(config.get_tenant().name, {})
    day = _today()
    tenant_data[day] = int(tenant_data.get(day, 0)) + int(tokens)
    _save(data)
    return tenant_data[day]
