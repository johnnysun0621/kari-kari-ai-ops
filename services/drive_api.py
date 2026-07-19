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
from pathlib import Path

logger = logging.getLogger(__name__)

_FOLDER_MIME = "application/vnd.google-apps.folder"
_services: dict[str, object] = {}


def _token_path() -> str:
    return os.getenv("GOOGLE_OAUTH_TOKEN_PATH", "credentials/token.json")


def is_configured() -> bool:
    """Drive 歸檔是否可用（token 已透過 scripts/authorize_drive.py 產生）。"""
    return Path(_token_path()).exists()


def _get_service():
    key = _token_path()
    service = _services.get(key)
    if service is None:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(key)
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        _services[key] = service
    return service


def reset_for_tests() -> None:
    _services.clear()


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
