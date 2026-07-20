"""作業歸檔的路徑邏輯（純函式積木）。

歸檔結構（2026-07-10 與 Johnny 拍板，取代 Kickoff 的課程→日期→藝人）：

    總資料夾/
    <藝人>專區/                ← Artists.Google Drive 資料夾 存專區連結
        <任務類型夾>/<YYYY-MM>/<Task ID>_<任務名稱>/

優先序：Tasks.Google Drive Folder 有值 → 直接歸該資料夾；
否則 → 藝人專區 / 類型夾 / 月份夾 / 任務資料夾。

這個模組只算「該放哪」；實際的 Drive API 上傳（find-or-create 資料夾、
上傳檔案、回寫 File ID）等 OAuth 憑證到位後在 drive_api 模組實作。
"""
import re
from datetime import datetime

from services import repositories

# 任務類型 → 專區子資料夾名稱。租戶可覆寫（TenantConfig 之後可注入）。
# 預設：每個類型一個夾（2026-07-10 決策），對不上的進「其他」。
DEFAULT_TYPE_FOLDERS = {
    "課程作業": "課程作業",
    "創作交付": "創作交付",
    "企劃提案": "企劃提案",
    "業配內容": "業配內容",
    "影片素材": "影片素材",
    "錄音": "錄音",
    "演出": "演出",
    "行政": "行政",
    "課程場次": "課程場次",
    "會議溝通": "會議溝通",
    "發布上架": "發布上架",
}
FALLBACK_TYPE_FOLDER = "其他"
REVIEW_FEEDBACK_FOLDER = "老師回饋"
SUBMISSION_FOLDER = "提交作業"
REVIEW_FOLDER = "老師回饋"

_FOLDER_URL_PATTERNS = [
    re.compile(r"drive\.google\.com/drive/(?:u/\d+/)?folders/([\w-]+)"),
    re.compile(r"drive\.google\.com/open\?id=([\w-]+)"),
]


def extract_drive_folder_id(url: str) -> str | None:
    """從 Drive 資料夾連結取出 folder ID；不是資料夾連結回 None。"""
    if not url:
        return None
    for pattern in _FOLDER_URL_PATTERNS:
        m = pattern.search(url)
        if m:
            return m.group(1)
    return None


def type_folder_for_task(
    task_type: str | None,
    type_folders: dict[str, str] | None = None,
) -> str:
    mapping = type_folders if type_folders is not None else DEFAULT_TYPE_FOLDERS
    return mapping.get(task_type or "", FALLBACK_TYPE_FOLDER)


def task_folder_name(
    task: "repositories.TaskItem",
    fallback_date: datetime | None = None,
) -> str:
    """Return a readable task folder name without exposing an internal ID."""
    name = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", task.name or "未命名任務")
    name = re.sub(r"\s+", " ", name).strip(" ._") or "未命名任務"
    if task.end:
        date_label = f"截止{task.end.strftime('%Y-%m-%d')}"
    else:
        date_value = task.created_at or fallback_date or datetime.now()
        date_label = f"建立{date_value.strftime('%Y-%m-%d')}"
    return f"{name[:70].rstrip(' ._')}｜{date_label}"


def archive_path_for_submission(
    task: "repositories.TaskItem",
    now: datetime,
    type_folders: dict[str, str] | None = None,
) -> list[str]:
    """回傳專區底下的任務資料夾路徑，例如 ["課程作業", "2026-07", "編曲作業｜建立2026-07-09"]。

    呼叫端負責：任務有 Google Drive Folder 時不呼叫這裡（直接用任務指定夾）。
    """
    return [
        type_folder_for_task(task.task_type, type_folders),
        now.strftime("%Y-%m"),
        task_folder_name(task, now),
    ]


def archive_path_for_review_feedback(
    task: "repositories.TaskItem", now: datetime,
) -> list[str]:
    """老師回饋放在同一藝人專區下的任務資料夾。"""
    return [
        type_folder_for_task(task.task_type),
        now.strftime("%Y-%m"),
        task_folder_name(task, now),
        REVIEW_FOLDER,
    ]


def review_feedback_folder_name(created_at: datetime) -> str:
    return f"回饋_{created_at.strftime('%Y%m%d_%H%M')}"


def resolve_archive_target(
    task: "repositories.TaskItem",
    artist_zone_url: str,
    now: datetime,
    type_folders: dict[str, str] | None = None,
) -> tuple[str, list[str]] | None:
    """決定歸檔目標：(基底資料夾 ID, 底下要 find-or-create 的相對路徑段)。

    - 任務有指定 Google Drive Folder → (該夾 ID, []) 直接歸檔
    - 否則 → (藝人專區 ID, [類型夾, YYYY-MM, 任務名稱｜截止日期])
    - 兩者都拿不到 folder ID → None（呼叫端 fallback 成「請貼連結」）
    """
    task_folder_id = extract_drive_folder_id(getattr(task, "drive_folder_url", "") or "")
    if task_folder_id:
        return task_folder_id, []
    zone_id = extract_drive_folder_id(artist_zone_url)
    if zone_id:
        return zone_id, archive_path_for_submission(task, now, type_folders)
    return None


def resolve_review_feedback_target(
    artist_zone_url: str,
    now: datetime,
    task: "repositories.TaskItem | None" = None,
) -> tuple[str, list[str]] | None:
    """決定老師批改檔歸檔目標；未傳任務時保留舊路徑相容。"""
    zone_id = extract_drive_folder_id(artist_zone_url)
    if not zone_id:
        return None
    if task is None:
        return zone_id, [REVIEW_FEEDBACK_FOLDER, now.strftime("%Y-%m")]
    return zone_id, archive_path_for_review_feedback(task, now)
