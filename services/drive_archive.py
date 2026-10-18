"""作業歸檔的路徑邏輯（純函式積木）。

歸檔結構（2026-10-13 改版，規則 M4-07；取代 2026-07-10 的「任務類型夾／月份夾」）：

    總資料夾/
    <藝人>專區/                       ← Artists.Google Drive 資料夾 存專區連結
        <專案名稱>/                   ← 任務「關聯專案」的專案名稱；沒掛專案 → 未分專案
            <YYYY.MM.DD_任務名稱>/
                <任務名稱_YYYY.MM.DD_ver.N>/
                老師回饋/老師回饋_YYYY.MM.DD_ver.N/

為什麼改看專案：任務類型大多沒填（2026-10-13 實撈 173 筆中 107 筆），而且客戶會把
案子名稱當成任務類型、隨時新增或改名，依它分夾幾乎全進「其他」。專案則 97% 都有掛，
控方在 Notion 看到的專案就是 Drive 上的資料夾，不必另外維護對照表。

優先序：Tasks.Google Drive Folder 有值 → 直接歸該資料夾（第一次提交後會回寫，所以
同一任務之後的提交不受專案改名影響）；否則 → 藝人專區 / 專案夾 / 任務夾。
資料夾以名稱 find-or-create，專案改名等於開新夾——規則頁要求專案建好後盡量不改名。
"""
import re
from datetime import datetime

from services import repositories

UNASSIGNED_PROJECT_FOLDER = "未分專案"
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


def project_folder_name(project_name: str | None) -> str:
    """專案夾名稱：照 Notion 專案名稱，清掉 Drive 不接受的字元；沒有專案 → 未分專案。"""
    if not (project_name or "").strip():
        return UNASSIGNED_PROJECT_FOLDER
    return _safe_versioned_folder_item(project_name, UNASSIGNED_PROJECT_FOLDER)


def task_folder_name(
    task: "repositories.TaskItem",
    fallback_date: datetime | None = None,
) -> str:
    """Return a date-first task folder name without exposing an internal ID."""
    name = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", task.name or "未命名任務")
    name = re.sub(r"\s+", " ", name).strip(" ._") or "未命名任務"
    if task.end:
        date_label = f"截止{task.end.strftime('%Y-%m-%d')}"
    else:
        date_value = task.created_at or fallback_date or datetime.now()
        date_label = f"建立{date_value.strftime('%Y-%m-%d')}"
    date_value = date_label.removeprefix("截止").removeprefix("建立")
    return f"{date_value.replace('-', '.')}_{name[:70].rstrip(' ._')}"


def _safe_versioned_folder_item(value: str, fallback: str = "未命名項目") -> str:
    value = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", value or fallback)
    value = re.sub(r"\s+", " ", value).strip(" ._") or fallback
    return value[:70].rstrip(" ._") or fallback


def versioned_folder_prefix(item_name: str, created_at: datetime) -> str:
    """Return `<item>_YYYY.MM.DD_ver.` for a submission/review folder."""
    item = _safe_versioned_folder_item(item_name)
    return f"{item}_{created_at.strftime('%Y.%m.%d')}_ver."


def next_versioned_folder_name(existing_names: list[str], prefix: str) -> str:
    """Choose the next numeric version among sibling folders sharing a prefix."""
    versions: list[int] = []
    for name in existing_names:
        if not name.startswith(prefix):
            continue
        suffix = name[len(prefix):]
        if suffix.isdigit():
            versions.append(int(suffix))
    return f"{prefix}{max(versions, default=0) + 1}"


def archive_path_for_submission(
    task: "repositories.TaskItem",
    now: datetime,
    project_name: str | None = None,
) -> list[str]:
    """回傳專區底下的任務資料夾路徑，例如 ["方晨2610 (發文）", "2026.10.16_提供稿件"]。

    呼叫端負責：任務有 Google Drive Folder 時不呼叫這裡（直接用任務指定夾）。
    """
    return [project_folder_name(project_name), task_folder_name(task, now)]


def archive_path_for_review_feedback(
    task: "repositories.TaskItem",
    now: datetime,
    project_name: str | None = None,
) -> list[str]:
    """老師回饋放在同一藝人專區下的任務資料夾。"""
    return [*archive_path_for_submission(task, now, project_name), REVIEW_FOLDER]


def review_feedback_folder_name(created_at: datetime, version: int = 1) -> str:
    """Compatibility helper for callers that already know the target version."""
    return f"{versioned_folder_prefix('老師回饋', created_at)}{version}"


def resolve_archive_target(
    task: "repositories.TaskItem",
    artist_zone_url: str,
    now: datetime,
    project_name: str | None = None,
) -> tuple[str, list[str]] | None:
    """決定歸檔目標：(基底資料夾 ID, 底下要 find-or-create 的相對路徑段)。

    - 任務有指定 Google Drive Folder → (該夾 ID, []) 直接歸檔
    - 否則 → (藝人專區 ID, [專案夾, 任務夾])
    - 兩者都拿不到 folder ID → None（呼叫端提示聯絡控方）
    """
    task_folder_id = extract_drive_folder_id(getattr(task, "drive_folder_url", "") or "")
    if task_folder_id:
        return task_folder_id, []
    zone_id = extract_drive_folder_id(artist_zone_url)
    if zone_id:
        return zone_id, archive_path_for_submission(task, now, project_name)
    return None


def resolve_review_feedback_target(
    artist_zone_url: str,
    now: datetime,
    task: "repositories.TaskItem | None" = None,
    project_name: str | None = None,
) -> tuple[str, list[str]] | None:
    """決定老師批改檔歸檔目標。

    任務已回寫 Google Drive Folder（單一藝人任務第一次提交後）→ 直接放在那個任務夾的
    老師回饋底下，專案事後改名也不會跑到別的夾；否則照專區路徑算。未傳任務時保留舊路徑相容。
    """
    task_folder_id = extract_drive_folder_id(getattr(task, "drive_folder_url", "") or "") if task else None
    if task_folder_id:
        return task_folder_id, [REVIEW_FOLDER]
    zone_id = extract_drive_folder_id(artist_zone_url)
    if not zone_id:
        return None
    if task is None:
        return zone_id, [REVIEW_FEEDBACK_FOLDER, now.strftime("%Y-%m")]
    return zone_id, archive_path_for_review_feedback(task, now, project_name)
