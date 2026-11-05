"""Audit Log 只在 Notion 留 90 天（M7-09，Johnny 2026-11-05 定）。

超過保留天數的紀錄：先寫進本機 CSV（`archive/audit_log/YYYY-MM.csv`，依建立月份分檔），
寫好、存到硬碟之後，才把 Notion 那一筆移到垃圾桶。順序不能反：先刪後存，存失敗就永遠找不回來。

- 不上傳雲端（Johnny 定）。`archive/` 在 .gitignore 裡：紀錄有藝人名稱和 Discord ID，repo 是公開的。
- 每次只處理一批（BATCH_SIZE），舊資料多的時候分好幾輪清完，不會一次把 Notion 額度用光。
- 移垃圾桶失敗的那筆留在 Notion，下一輪會再撈到；CSV 用 page_id 去重，不會重複寫。
- 小律只寫 Audit Log、不讀它，所以清掉舊紀錄不影響任何功能。
"""
import csv
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import config
from services import notion_api

logger = logging.getLogger(__name__)

ARCHIVE_DIR = Path("archive/audit_log")
BATCH_SIZE = 200

# 固定欄位順序：Notion 之後加欄位也不會讓同一個 CSV 前後欄位對不上；沒列到的欄位收進「其他欄位」
COLUMNS = (
    "page_id", "建立時間", "事件名稱", "操作時間", "操作類型", "操作者", "來源系統", "結果",
    "Discord User ID", "目標資料庫", "目標頁面 URL", "前值", "後值", "錯誤訊息", "Log ID",
)
EXTRA_COLUMN = "其他欄位"


@dataclass
class RetentionResult:
    found: int = 0
    archived: int = 0
    trashed: int = 0
    failed: int = 0


def property_text(prop: dict) -> str:
    """把一個 Notion 欄位轉成純文字。看不懂的型別回空字串，不讓整批失敗。"""
    kind = prop.get("type")
    value = prop.get(kind)
    if kind in ("title", "rich_text"):
        return "".join(part.get("plain_text", "") for part in value or [])
    if kind in ("select", "status"):
        return (value or {}).get("name", "")
    if kind == "multi_select":
        return "、".join(option.get("name", "") for option in value or [])
    if kind == "date":
        if not value:
            return ""
        return value.get("start", "") + (f" → {value['end']}" if value.get("end") else "")
    if kind == "unique_id":
        if not value:
            return ""
        return f"{value.get('prefix') or ''}{'-' if value.get('prefix') else ''}{value.get('number', '')}"
    if kind in ("url", "email", "phone_number", "created_time", "last_edited_time"):
        return value or ""
    if kind in ("number", "checkbox"):
        return "" if value is None else str(value)
    if kind == "relation":
        return "、".join(item.get("id", "") for item in value or [])
    if kind == "formula":
        inner = (value or {}).get((value or {}).get("type"))
        return "" if inner is None else str(inner)
    return ""


def row_for(page: dict) -> dict[str, str]:
    props = page.get("properties", {})
    row = {"page_id": page["id"], "建立時間": page.get("created_time", "")}
    extras = {}
    for name, prop in props.items():
        text = property_text(prop)
        if name in COLUMNS:
            row[name] = text
        elif text:
            extras[name] = text
    row[EXTRA_COLUMN] = json.dumps(extras, ensure_ascii=False) if extras else ""
    return row


def month_of(page: dict) -> str:
    created = datetime.fromisoformat(page["created_time"].replace("Z", "+00:00"))
    return created.astimezone(config.TZ).strftime("%Y-%m")


def _existing_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open(encoding="utf-8-sig", newline="") as f:
        return {row["page_id"] for row in csv.DictReader(f) if row.get("page_id")}


def write_archive(pages: list[dict], archive_dir: Path | None = None) -> set[str]:
    """寫進每月的 CSV，回傳「已經安全存在硬碟上」的 page_id（含之前就存過的）。

    用 utf-8-sig：Excel 直接打開中文不會變亂碼。寫完 flush＋fsync 才回傳，確定真的落地了。
    """
    archive_dir = archive_dir or ARCHIVE_DIR
    archive_dir.mkdir(parents=True, exist_ok=True)
    by_month: dict[str, list[dict]] = {}
    for page in pages:
        by_month.setdefault(month_of(page), []).append(page)
    saved: set[str] = set()
    for month, month_pages in sorted(by_month.items()):
        path = archive_dir / f"{month}.csv"
        existing = _existing_ids(path)
        new_rows = [row_for(page) for page in month_pages if page["id"] not in existing]
        if new_rows:
            is_new = not path.exists()
            with path.open("a", encoding="utf-8-sig" if is_new else "utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=[*COLUMNS, EXTRA_COLUMN])
                if is_new:
                    writer.writeheader()
                writer.writerows(new_rows)
                f.flush()
                os.fsync(f.fileno())
        saved |= existing | {row["page_id"] for row in new_rows}
    return saved


async def fetch_expired(now: datetime, retention_days: int, limit: int = BATCH_SIZE) -> list[dict]:
    cutoff = now - timedelta(days=retention_days)
    return await notion_api.query_data_source(
        config.DS_AUDIT_LOG,
        filter_={"timestamp": "created_time", "created_time": {"before": cutoff.isoformat()}},
        sorts=[{"timestamp": "created_time", "direction": "ascending"}],
        limit=limit,
    )


async def trash_page(page_id: str) -> None:
    try:
        await notion_api.update_page(page_id, archived=True)
    except notion_api.NotionAPIError as e:
        if e.status_code == 400 and ("already archived" in str(e) or "is archived" in str(e)):
            return
        raise


async def run_once(
    now: datetime | None = None,
    *,
    retention_days: int | None = None,
    archive_dir: Path | None = None,
    dry_run: bool = False,
    limit: int = BATCH_SIZE,
) -> RetentionResult:
    """清一批：撈超過保留天數的紀錄 → 寫 CSV → 只把確定存好的移到垃圾桶。"""
    now = now or datetime.now(config.TZ)
    days = config.get_tenant().audit_retention_days if retention_days is None else retention_days
    result = RetentionResult()
    if days <= 0:
        return result
    pages = [p for p in await fetch_expired(now, days, limit) if not (p.get("archived") or p.get("in_trash"))]
    result.found = len(pages)
    if dry_run or not pages:
        return result
    saved = write_archive(pages, archive_dir)
    result.archived = len(saved & {page["id"] for page in pages})
    for page in pages:
        if page["id"] not in saved:
            result.failed += 1
            continue
        try:
            await trash_page(page["id"])
            result.trashed += 1
        except Exception:
            result.failed += 1
            logger.exception("Audit Log 舊紀錄移到垃圾桶失敗（已存進 CSV，下一輪再試）：%s", page["id"])
    return result
