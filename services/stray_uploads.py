"""藝人直接傳到 Drive、沒走小律交件的檔案（M4-23，Johnny 2026-11-11 定）。

2026-11-09 方晨在自己的專區建「週練習week6」直接上傳 17 個檔案，系統不知道，week2 顯示已逾期。

- 每天 12:00、21:00 掃每位藝人的 Drive 專區，找「不在小律交件資料夾（*_ver.N）裡」的新檔案
- 分組：藝人自己建的資料夾整個算一組；直接放在專區或小律資料夾裡的散檔，依所在資料夾算一組
- 怎麼分辨：小律建的資料夾擁有者是小律帳號；擁有者是別人的資料夾，就是藝人自己建的
- 最後一個檔案上傳超過 30 分鐘才問（避免還在傳）；只看最近 14 天
- 私訊藝人選任務 → 小律開標準資料夾、整組搬進去、照一般交件寫 Notion（`record_folder_submission`）
- 狀態存在本機 `.state/stray_uploads.json`；營運早報列出還沒處理的
"""
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_PATH = Path(".state/stray_uploads.json")
SCAN_HOURS = (12, 21)
LOOKBACK_DAYS = 14
QUIET_MINUTES = 30
MAX_GROUP_DEPTH = 4
MAX_STRUCTURE_DEPTH = 3
MAX_TASK_OPTIONS = 24
FOLDER_MIME = "application/vnd.google-apps.folder"
VER_RE = re.compile(r"_ver\.\d+$")
SKIP_FOLDER_NAMES = {"老師回饋"}

# 狀態：asked 已私訊等回覆／dm_failed 私訊不通／no_task 沒有可交的作業／ignored 藝人說不是作業／submitted 已補登
PENDING_STATUSES = {"asked", "dm_failed", "no_task"}
STATUS_TEXT = {"asked": "已私訊，還沒回", "dm_failed": "私訊不通", "no_task": "沒有可以交的作業"}


# ---------- 狀態檔 ----------

_state: dict | None = None


def reset_for_tests() -> None:
    global _state
    _state = None


def _load() -> dict:
    global _state
    if _state is None:
        try:
            raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            _state = raw if isinstance(raw, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
            _state = {}
        _state.setdefault("groups", {})
        _state.setdefault("meta", {})
    return _state


def _save() -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_load(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def get_group(key: str) -> dict | None:
    entry = _load()["groups"].get(key)
    return dict(entry) if entry else None


def update_group(key: str, **fields) -> None:
    _load()["groups"].setdefault(key, {}).update(fields)
    _save()


def meta(key: str, default=None):
    return _load()["meta"].get(key, default)


def set_meta(key: str, value) -> None:
    _load()["meta"][key] = value
    _save()


def scan_slot(now: datetime) -> str | None:
    """現在該跑哪一輪（例如 2026-11-11@21）；這一輪跑過了或還沒到就回 None。"""
    due = [hour for hour in SCAN_HOURS if now.hour >= hour]
    if not due:
        return None
    slot = f"{now.date().isoformat()}@{due[-1]}"
    return None if meta("last_slot") == slot else slot


# ---------- 掃描 ----------

@dataclass
class StrayGroup:
    key: str  # f{資料夾 id}＝藝人自己建的資料夾；l{資料夾 id}＝放在這個資料夾裡的散檔
    kind: str  # folder / loose
    folder_id: str
    name: str
    location: str  # 在專區裡的位置，例如「方晨 2611 週練習/」
    file_ids: list[str] = field(default_factory=list)
    count: int = 0
    total_bytes: int = 0
    first_uploaded: str = ""
    last_uploaded: str = ""

    @property
    def total_mb(self) -> int:
        return round(self.total_bytes / 1048576)

    def label(self) -> str:
        return f"「{self.name}」" if self.kind == "folder" else f"直接放在「{self.name}」的檔案"


def _children(service, folder_id: str) -> list[dict]:
    items, token = [], None
    while True:
        result = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            pageSize=200, pageToken=token,
            fields="nextPageToken, files(id,name,mimeType,size,createdTime,modifiedTime,owners(me))",
            includeItemsFromAllDrives=True, supportsAllDrives=True,
        ).execute()
        items += result.get("files", [])
        token = result.get("nextPageToken")
        if not token:
            return items


def _owned_by_ritsu(item: dict) -> bool:
    owners = item.get("owners") or []
    return bool(owners and owners[0].get("me"))


def _uploaded_at(item: dict) -> str:
    return max(item.get("createdTime") or "", item.get("modifiedTime") or "")


def _all_files(service, folder_id: str, depth: int = 0) -> list[dict]:
    files = []
    for item in _children(service, folder_id):
        if item["mimeType"] == FOLDER_MIME:
            if depth < MAX_GROUP_DEPTH:
                files += _all_files(service, item["id"], depth + 1)
        else:
            files.append(item)
    return files


def _make_group(key: str, kind: str, folder_id: str, name: str, location: str, files: list[dict]) -> StrayGroup:
    times = sorted(_uploaded_at(f) for f in files)
    return StrayGroup(
        key=key, kind=kind, folder_id=folder_id, name=name, location=location,
        file_ids=[f["id"] for f in files], count=len(files),
        total_bytes=sum(int(f.get("size") or 0) for f in files),
        first_uploaded=min(f.get("createdTime") or "" for f in files), last_uploaded=times[-1],
    )


def scan_zone_sync(service, zone_id: str, zone_name: str = "專區最上層") -> list[StrayGroup]:
    """找出這個專區裡不在小律交件資料夾裡的檔案，分組回傳（不看時間，呼叫端再篩）。"""
    groups: list[StrayGroup] = []
    stack: list[tuple[str, str, str, int]] = [(zone_id, zone_name, "", 0)]
    while stack:
        folder_id, folder_name, location, depth = stack.pop()
        loose: list[dict] = []
        for item in _children(service, folder_id):
            name = item.get("name", "")
            if item["mimeType"] == FOLDER_MIME:
                if VER_RE.search(name) or name in SKIP_FOLDER_NAMES:
                    continue  # 小律的交件／批改檔資料夾，一般流程在管
                if _owned_by_ritsu(item):
                    if depth < MAX_STRUCTURE_DEPTH:
                        stack.append((item["id"], name, f"{location}{name}/", depth + 1))
                    continue
                files = _all_files(service, item["id"])
                if files:
                    groups.append(_make_group(f"f{item['id']}", "folder", item["id"], name, location, files))
            elif not _owned_by_ritsu(item):
                loose.append(item)
        if loose:
            groups.append(_make_group(f"l{folder_id}", "loose", folder_id, folder_name, location, loose))
    return groups


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def needs_asking(group: StrayGroup, now: datetime) -> bool:
    """這組要不要私訊：最近 14 天有上傳、最後一個檔案已經放了 30 分鐘、還沒處理過（或處理後又有新檔案）。"""
    last = _parse(group.last_uploaded)
    if last is None or last < now - timedelta(days=LOOKBACK_DAYS) or last > now - timedelta(minutes=QUIET_MINUTES):
        return False
    entry = get_group(group.key)
    if entry is None:
        return True
    if entry.get("status") in PENDING_STATUSES:
        return False
    handled_at = _parse(entry.get("handled_at") or "")
    return handled_at is not None and last > handled_at  # 說過不是作業或已補登，之後又傳了新檔案


def forget_missing(artist_page_id: str, found_keys: set[str]) -> None:
    """還在等回覆的組，這次掃描不見了（例如藝人自己搬進交件資料夾交了）→ 不再列在營運早報。"""
    changed = False
    for key, entry in _load()["groups"].items():
        if entry.get("artist_page_id") == artist_page_id and entry.get("status") in PENDING_STATUSES \
                and key not in found_keys:
            entry["status"] = "gone"
            changed = True
    if changed:
        _save()


# ---------- 給藝人選的任務 ----------

_HINT_TOKEN = re.compile(r"[a-z]{3,}|[一-鿿]{2,}")


def suggest_tasks(tasks, last_uploaded: str, hint: str = "") -> list:
    """可以交的作業排序：名稱跟資料夾名稱有共同關鍵字的在前，再依期限離上傳時間的遠近。

    2026-11-11 試掃：方晨的「週練習week6」只看期限，會把「11/10 穿搭轉場影片」排在「11/8 week2」前面；
    先比關鍵字（week）才排得對。
    """
    from services import repositories

    uploaded = _parse(last_uploaded) or datetime.now(timezone.utc)
    far = timedelta(days=3650)
    tokens = set(_HINT_TOKEN.findall((hint or "").lower()))

    def key(task):
        name = (task.name or "").lower()
        overlap = sum(1 for token in tokens if token in name)
        return (-overlap, abs(task.end - uploaded) if task.end else far)

    candidates = [task for task in tasks if repositories.is_task_submittable(task)]
    return sorted(candidates, key=key)[:MAX_TASK_OPTIONS]


def local_time(ts: str, tz) -> datetime | None:
    parsed = _parse(ts)
    return parsed.astimezone(tz) if parsed else None


def ask_text(group: StrayGroup, tz, has_tasks: bool) -> str:
    uploaded = local_time(group.last_uploaded, tz)
    when = f"{uploaded.month}/{uploaded.day}" if uploaded else "最近"
    lines = [
        "📂 **小律在你的 Drive 看到新檔案**",
        f"{group.label()}：{group.count} 個檔案，{when} 上傳",
    ]
    if has_tasks:
        lines += [
            "這是要交哪一筆作業呢？選好之後，小律會幫你放到正確的位置並交件，**不用重傳**。",
            "不是作業的話，按「不是作業，不用交」就好。",
        ]
    else:
        lines.append("目前沒有開放交件的作業，小律會轉告公司確認。不是作業的話，按「不是作業，不用交」就好。")
    return "\n".join(lines)


# ---------- 營運早報 ----------

def ops_lines(tz, mark_reported: bool = True) -> list[str]:
    """營運早報「📂 直接傳到 Drive、還沒登記交件」：還沒處理的每天列；已補登的列一次。"""
    lines = []
    changed = False
    for entry in _load()["groups"].values():
        status = entry.get("status")
        uploaded = local_time(entry.get("last_uploaded") or "", tz)
        when = f"{uploaded.month}/{uploaded.day} 上傳" if uploaded else ""
        where = entry.get("label") or entry.get("name", "")
        if status in PENDING_STATUSES:
            lines.append(f"{entry.get('artist_name', '')}｜{where}（{entry.get('count', 0)} 個，{when}）｜{STATUS_TEXT[status]}")
        elif status == "submitted" and not entry.get("reported"):
            lines.append(f"已補登：{entry.get('artist_name', '')}｜{where} → {entry.get('task_name', '')}")
            if mark_reported:
                entry["reported"] = True
                changed = True
    if changed:
        _save()
    return lines
