"""Google Drive 上傳（P0-3 檔案歸檔）。

責任邊界：只管 Drive 操作（找/建資料夾、上傳）。
「該放哪」的路徑邏輯在 drive_archive.py；Notion 寫入在 repositories.py。

googleapiclient 是 blocking 的——所有呼叫都包 asyncio.to_thread，
避免卡住 bot 的 event loop（Discord 互動 3 秒硬限）。
連線以 token 路徑為 key（每租戶一份，不做全域單例）。
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_FOLDER_MIME = "application/vnd.google-apps.folder"
_services: dict[str, object] = {}
_version_folder_locks: dict[str, asyncio.Lock] = {}


_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]


def _service_account_path() -> str:
    return os.getenv("GOOGLE_SERVICE_ACCOUNT_KEY_PATH", "credentials/service_account.json")


def _token_path() -> str:
    return os.getenv("GOOGLE_OAUTH_TOKEN_PATH", "credentials/token.json")


def _credential_path() -> str:
    """服務帳號優先於 OAuth 使用者授權。

    服務帳號沒有同意畫面、沒有 refresh token，不會像 OAuth 使用者授權那樣在
    Google Cloud 專案處於「測試中」狀態時 7 天不活動就失效（這個專案已經
    踩過這個坑，見 DRIVE_OAUTH_SETUP.md「常見問題」）。兩者都設定時服務帳號
    贏，方便從 OAuth 切換過去時不必刪舊檔案。
    """
    service_account_path = _service_account_path()
    if Path(service_account_path).exists():
        return service_account_path
    return _token_path()


def is_configured() -> bool:
    """Drive 歸檔是否可用（服務帳號金鑰或 OAuth token 其中之一已就緒）。"""
    return Path(_service_account_path()).exists() or Path(_token_path()).exists()


def _get_service():
    key = _credential_path()
    service = _services.get(key)
    if service is None:
        from googleapiclient.discovery import build

        if key == _service_account_path():
            from google.oauth2.service_account import Credentials as ServiceAccountCredentials

            creds = ServiceAccountCredentials.from_service_account_file(key, scopes=_DRIVE_SCOPES)
        else:
            from google.oauth2.credentials import Credentials

            creds = Credentials.from_authorized_user_file(key)
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        _services[key] = service
    return service


def reset_for_tests() -> None:
    _services.clear()
    _version_folder_locks.clear()


def _escape_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _find_or_create_folder_sync(service, parent_id: str, name: str) -> str:
    query = (
        f"'{parent_id}' in parents"
        f" and name = '{_escape_query_value(name)}'"
        f" and mimeType = '{_FOLDER_MIME}'"
        " and trashed = false"
    )
    result = service.files().list(q=query, fields="files(id)", pageSize=1).execute()
    files = result.get("files", [])
    if files:
        return files[0]["id"]
    created = service.files().create(
        body={"name": name, "mimeType": _FOLDER_MIME, "parents": [parent_id]},
        fields="id",
    ).execute()
    logger.info("Drive 建立資料夾：%s（parent=%s）", name, parent_id)
    return created["id"]


def _upload_sync(service, parent_id: str, filename: str, content: bytes, mime_type: str) -> dict:
    from googleapiclient.http import MediaInMemoryUpload

    media = MediaInMemoryUpload(
        content, mimetype=mime_type or "application/octet-stream", resumable=False
    )
    return service.files().create(
        body={"name": filename, "parents": [parent_id]},
        media_body=media,
        fields="id,webViewLink",
    ).execute()


def _folder_resource_sync(service, folder_id: str) -> dict:
    return service.files().get(
        fileId=folder_id,
        fields="id,name,webViewLink",
    ).execute()


def _list_child_folder_names_sync(service, parent_id: str) -> list[str]:
    query = (
        f"'{_escape_query_value(parent_id)}' in parents"
        f" and mimeType = '{_FOLDER_MIME}'"
        " and trashed = false"
    )
    result = service.files().list(
        q=query,
        fields="files(name)",
        pageSize=1000,
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
    ).execute()
    return [str(item.get("name") or "") for item in result.get("files", [])]


async def upload_submission_file(
    *,
    base_folder_id: str,
    path_segments: list[str],
    filename: str,
    content: bytes,
    mime_type: str = "",
) -> dict:
    """把檔案上傳到 base 資料夾底下的 path_segments 路徑（逐層 find-or-create）。

    回傳 Drive API 的 file 資源（含 id、webViewLink）。
    """
    def run() -> dict:
        service = _get_service()
        folder_id = base_folder_id
        for segment in path_segments:
            folder_id = _find_or_create_folder_sync(service, folder_id, segment)
        return _upload_sync(service, folder_id, filename, content, mime_type)

    return await asyncio.to_thread(run)


async def ensure_folder_path(
    *,
    base_folder_id: str,
    path_segments: list[str],
) -> dict:
    """Find-or-create a folder path and return the final Drive folder resource."""
    def run() -> dict:
        service = _get_service()
        folder_id = base_folder_id
        for segment in path_segments:
            folder_id = _find_or_create_folder_sync(service, folder_id, segment)
        return _folder_resource_sync(service, folder_id)

    return await asyncio.to_thread(run)


async def ensure_next_version_folder(
    *,
    base_folder_id: str,
    parent_path_segments: list[str],
    name_prefix: str,
) -> dict:
    """Create and return the next `ver.N` folder below a resolved parent path."""
    def run() -> dict:
        from services import drive_archive

        service = _get_service()
        parent_id = base_folder_id
        for segment in parent_path_segments:
            parent_id = _find_or_create_folder_sync(service, parent_id, segment)
        existing_names = _list_child_folder_names_sync(service, parent_id)
        name = drive_archive.next_versioned_folder_name(existing_names, name_prefix)
        folder_id = _find_or_create_folder_sync(service, parent_id, name)
        return _folder_resource_sync(service, folder_id)

    lock_key = "\x1f".join([base_folder_id, *parent_path_segments, name_prefix])
    lock = _version_folder_locks.setdefault(lock_key, asyncio.Lock())
    async with lock:
        return await asyncio.to_thread(run)


def _list_recent_files_sync(service, folder_id: str, since: datetime) -> list[dict]:
    # Drive 的上傳／同步時間偶爾會比 Discord 建立入口晚幾秒，或在
    # 移入資料夾時保留原始 createdTime；保留小幅容錯，並用 modifiedTime
    # 覆蓋這類合法上傳情境。
    search_since = since - timedelta(minutes=10)
    since_utc = search_since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    query = (
        f"'{_escape_query_value(folder_id)}' in parents"
        " and trashed = false"
        " and mimeType != 'application/vnd.google-apps.folder'"
        f" and (createdTime >= '{since_utc}' or modifiedTime >= '{since_utc}')"
    )
    result = service.files().list(
        q=query,
        orderBy="createdTime desc",
        pageSize=100,
        fields="files(id,name,size,webViewLink,createdTime,modifiedTime,mimeType)",
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
    ).execute()
    return result.get("files", [])


async def list_recent_files(folder_id: str, since: datetime) -> list[dict]:
    """列出資料夾內指定時間後新增的檔案，供藝人上傳驗證。"""
    return await asyncio.to_thread(
        lambda: _list_recent_files_sync(_get_service(), folder_id, since)
    )
