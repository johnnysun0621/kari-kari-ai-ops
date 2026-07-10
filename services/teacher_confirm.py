"""老師外部確認連結：token 簽名／驗證／一次性使用（Phase 2）。

token = base64url(payload) + "." + base64url(HMAC-SHA256(secret, payload))
payload = {"rid": 排課需求 page_id, "exp": unix 過期時間}

- secret 來自 TenantConfig.teacher_link_secret（未設定＝功能停用，排課流程維持人工）
- 一次性：成功送出後 token 指紋存 .state/teacher_confirm.json（沿用 time_collection 模式），
  重啟不失效；過期指紋於載入時修剪
"""
import base64
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import config

logger = logging.getLogger(__name__)

_STATE_PATH = Path(".state/teacher_confirm.json")
_used_tokens: dict[str, int] = {}  # 指紋 -> 過期時間（供修剪）
_state_loaded = False

TOKEN_TTL_HOURS = 24  # 對齊 SLA：老師確認 24 小時內


def is_enabled() -> bool:
    """有 secret 就啟用 web 確認服務；沒有就整條功能優雅停用。"""
    return bool(config.get_tenant().teacher_link_secret)


def base_url() -> str:
    return config.get_tenant().teacher_link_base_url.rstrip("/")


def can_generate_links() -> bool:
    """產生連結還需要對外網址（Tunnel）；沒有網址時比對流程不顯示連結。"""
    return is_enabled() and bool(base_url())


def _secret_bytes() -> bytes:
    return config.get_tenant().teacher_link_secret.encode("utf-8")


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()[:24]


def sign_token(request_page_id: str, expires_at_ts: int) -> str:
    payload = json.dumps(
        {"rid": request_page_id, "exp": int(expires_at_ts)}, separators=(",", ":")
    ).encode("utf-8")
    sig = hmac.new(_secret_bytes(), payload, hashlib.sha256).digest()
    return f"{_b64e(payload)}.{_b64e(sig)}"


@dataclass(frozen=True)
class TokenCheck:
    ok: bool
    request_page_id: str = ""
    expires_at_ts: int = 0
    error: str = ""  # "invalid" | "expired" | "used"


def verify_token(token: str, now_ts: int) -> TokenCheck:
    """驗證順序：格式/簽名 → 過期 → 已使用。"""
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        payload = _b64d(payload_b64)
        expected = hmac.new(_secret_bytes(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64d(sig_b64)):
            return TokenCheck(ok=False, error="invalid")
        data = json.loads(payload)
        rid, exp = str(data["rid"]), int(data["exp"])
    except Exception:
        return TokenCheck(ok=False, error="invalid")
    if now_ts > exp:
        return TokenCheck(ok=False, error="expired")
    _load_state()
    if _fingerprint(token) in _used_tokens:
        return TokenCheck(ok=False, error="used")
    return TokenCheck(ok=True, request_page_id=rid, expires_at_ts=exp)


def mark_token_used(token: str, expires_at_ts: int) -> None:
    _load_state()
    _used_tokens[_fingerprint(token)] = int(expires_at_ts)
    _save_state()


def unmark_token(token: str) -> None:
    """流程失敗時還原一次性標記，讓老師可重試（web 端專用）。"""
    _load_state()
    _used_tokens.pop(_fingerprint(token), None)
    _save_state()


def confirm_url(token: str) -> str:
    return f"{base_url()}/confirm/{token}"


# ---------- 一次性狀態持久化（bot 本機狀態，非營運資料）----------

def _load_state() -> None:
    global _state_loaded
    if _state_loaded:
        return
    _state_loaded = True
    try:
        raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return
    import time as _time
    now = int(_time.time())
    for fp, exp in raw.get("used", {}).items():
        if int(exp) >= now:  # 已過期的 token 本來就進不來，指紋不用留
            _used_tokens[fp] = int(exp)


def _save_state() -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(
            json.dumps({"used": _used_tokens}, indent=1), encoding="utf-8"
        )
    except OSError:
        logger.exception("老師確認狀態檔寫入失敗（重啟後一次性保護可能失效）")


def reload_state_for_tests() -> None:
    global _state_loaded
    _used_tokens.clear()
    _state_loaded = False
