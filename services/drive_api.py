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


def drop_cached_service() -> None:
    """授權失效時丟掉記憶體裡的連線，重新授權寫入新 token.json 後不必重啟 bot 就會讀到。"""
    _services.pop(_credential_path(), None)


async def check_folder_access(folder_id: str) -> dict:
    """讀一次資料夾（每日健康檢查用）；授權失效、資料夾不見或沒權限都會 raise。"""
    return await asyncio.to_thread(lambda: _folder_resource_sync(_get_service(), folder_id))


def _escape_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _find_or_create_folder_sync(service, parent_id: str, name: str) -> str:
    query = (
        f"'{parent_id}' in parents"
        f" and name = '{_escape_query_value(name)}'"
        f" and mimeType = '{_FOLDER_MIME}'"
        " and trashed = false"
    )
    result = service.files().list(
        q=query,
        fields="files(id)",
        pageSize=1,
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
    ).execute()
    files = result.get("files", [])
    if files:
        return files[0]["id"]
    created = service.files().create(
        body={"name": name, "mimeType": _FOLDER_MIME, "parents": [parent_id]},
        fields="id",
        supportsAllDrives=True,
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
        supportsAllDrives=True,
    ).execute()


def _folder_resource_sync(service, folder_id: str) -> dict:
    return service.files().get(
        fileId=folder_id,
        fields="id,name,webViewLink",
        supportsAllDrives=True,
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


MAX_SUBFOLDER_DEPTH = 3
MAX_LISTED_FILES = 200


def _list_recent_files_sync(service, folder_id: str, since: datetime) -> list[dict]:
    """列出資料夾（含子資料夾）裡指定時間後新增的檔案。

    2026-11-10：藝人在交件資料夾裡自己分了「歌名」子資料夾，原本只看最外層，
    15 個檔案都在卻回「找不到」。子資料夾往下找 3 層，最多 200 個檔案；
    子資料夾裡的檔案 `path` 會帶資料夾名稱，例如「ソラニン/IMG_6786.MOV」。
    """
    # Drive 的上傳／同步時間偶爾會比 Discord 建立入口晚幾秒，或在
    # 移入資料夾時保留原始 createdTime；保留小幅容錯，並用 modifiedTime
    # 覆蓋這類合法上傳情境。
    search_since = since - timedelta(minutes=10)
    since_utc = search_since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    found: list[dict] = []
    queue: list[tuple[str, str, int]] = [(folder_id, "", 0)]
    while queue and len(found) < MAX_LISTED_FILES:
        current, prefix, depth = queue.pop(0)
        query = (
            f"'{_escape_query_value(current)}' in parents"
            " and trashed = false"
            f" and (mimeType = '{_FOLDER_MIME}'"
            f" or createdTime >= '{since_utc}' or modifiedTime >= '{since_utc}')"
        )
        page_token = None
        while True:
            result = service.files().list(
                q=query,
                orderBy="createdTime desc",
                pageSize=100,
                pageToken=page_token,
                fields="nextPageToken, files(id,name,size,webViewLink,createdTime,modifiedTime,mimeType)",
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            ).execute()
            for item in result.get("files", []):
                if item.get("mimeType") == _FOLDER_MIME:
                    if depth < MAX_SUBFOLDER_DEPTH:
                        queue.append((item["id"], f"{prefix}{item.get('name', '')}/", depth + 1))
                    continue
                item["path"] = f"{prefix}{item.get('name', '')}"
                found.append(item)
            page_token = result.get("nextPageToken")
            if not page_token or len(found) >= MAX_LISTED_FILES:
                break
    return found[:MAX_LISTED_FILES]


def _move_item_sync(service, item_id: str, new_parent_id: str) -> None:
    current = service.files().get(fileId=item_id, fields="parents", supportsAllDrives=True).execute()
    old_parents = ",".join(current.get("parents") or [])
    service.files().update(
        fileId=item_id, addParents=new_parent_id, removeParents=old_parents,
        fields="id,parents", supportsAllDrives=True,
    ).execute()


async def move_item(item_id: str, new_parent_id: str) -> None:
    """把檔案或資料夾搬到另一個資料夾（M4-23 補登）。

    藝人擁有的檔案也搬得動、搬完擁有者不變（2026-11-11 在 Johnny（測試）專區實測）。
    """
    await asyncio.to_thread(lambda: _move_item_sync(_get_service(), item_id, new_parent_id))


def _get_files_sync(service, file_ids: list[str]) -> list[dict]:
    files = []
    for file_id in file_ids:
        try:
            item = service.files().get(
                fileId=file_id, fields="id,name,size,webViewLink,createdTime,modifiedTime,mimeType",
                supportsAllDrives=True,
            ).execute()
        except Exception:
            logger.warning("讀不到 Drive 檔案：%s", file_id)
            continue
        item["path"] = item.get("name", "")
        files.append(item)
    return files


async def get_files(file_ids: list[str]) -> list[dict]:
    return await asyncio.to_thread(lambda: _get_files_sync(_get_service(), file_ids))


async def list_recent_files(folder_id: str, since: datetime) -> list[dict]:
    """列出資料夾（含子資料夾）內指定時間後新增的檔案，供藝人上傳驗證。"""
    return await asyncio.to_thread(
        lambda: _list_recent_files_sync(_get_service(), folder_id, since)
    )
