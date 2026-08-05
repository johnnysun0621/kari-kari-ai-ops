"""LLM 使用量記錄與每日護欄；只保存 token 數，不保存問題或課程內容。"""
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import config

STATE_PATH = Path(".state/llm_usage.json")


@dataclass(frozen=True)
class LimitStatus:
    allowed: bool
    system_remaining: int | None
    artist_remaining: int | None


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


def _day_bucket(data: dict) -> dict:
    value = data.get(config.get_tenant().name, {}).get(_today(), {})
    if isinstance(value, (int, float)):
        return {"total": int(value), "users": {}}
    if not isinstance(value, dict):
        return {"total": 0, "users": {}}
    users = value.get("users", {})
    return {
        "total": int(value.get("total", 0)),
        "users": users if isinstance(users, dict) else {},
    }


def daily_total(user_key: str | None = None) -> int:
    data = _load()
    bucket = _day_bucket(data)
    if user_key is None:
        return bucket["total"]
    return int(bucket["users"].get(str(user_key), 0))


def _remaining(limit: int, used: int) -> int | None:
    return None if limit <= 0 else max(0, limit - used)


def limit_status(user_key: str | None = None) -> LimitStatus:
    tenant = config.get_tenant()
    system_remaining = _remaining(tenant.llm_daily_token_limit, daily_total())
    artist_limit = getattr(tenant, "llm_per_artist_daily_token_limit", 0)
    artist_remaining = (
        _remaining(artist_limit, daily_total(user_key)) if user_key else None
    )
    return LimitStatus(
        allowed=(system_remaining is None or system_remaining > 0)
        and (artist_remaining is None or artist_remaining > 0),
        system_remaining=system_remaining,
        artist_remaining=artist_remaining,
    )


def within_daily_limit(user_key: str | None = None) -> bool:
    return limit_status(user_key).allowed


def record(usage: dict | None, user_key: str | None = None) -> int:
    """記錄 API 回傳的 total_tokens；回傳今日累計。"""
    if not usage:
        return daily_total()
    tokens = usage.get("total_tokens")
    if not isinstance(tokens, (int, float)):
        return daily_total()
    data = _load()
    tenant_data = data.setdefault(config.get_tenant().name, {})
    day = _today()
    bucket = _day_bucket(data)
    bucket["total"] += int(tokens)
    if user_key:
        key = str(user_key)
        bucket["users"][key] = int(bucket["users"].get(key, 0)) + int(tokens)
    tenant_data[day] = bucket
    _save(data)
    return bucket["total"]
