"""手動清 Audit Log 舊紀錄（M7-09）。平常 bot 每小時會自己清一批，不需要跑這支。

用法（在專案根目錄）：
    .venv\\Scripts\\python.exe scripts\\archive_audit_log.py --dry-run   只看有幾筆會被清，不動任何東西
    .venv\\Scripts\\python.exe scripts\\archive_audit_log.py             清一批（存 CSV → 移到 Notion 垃圾桶）
    .venv\\Scripts\\python.exe scripts\\archive_audit_log.py --all       一直清到沒有為止
"""
import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from services import audit_retention, notion_api  # noqa: E402


async def main(dry_run: bool, run_all: bool) -> None:
    days = config.get_tenant().audit_retention_days
    if days <= 0:
        print("AUDIT_RETENTION_DAYS=0，清理已停用。")
        return
    archive_dir = ROOT / audit_retention.ARCHIVE_DIR
    total = audit_retention.RetentionResult()
    try:
        while True:
            result = await audit_retention.run_once(
                retention_days=days, archive_dir=archive_dir, dry_run=dry_run,
            )
            total.found += result.found
            total.archived += result.archived
            total.trashed += result.trashed
            total.failed += result.failed
            if dry_run:
                more = "（至少這麼多，一次最多看 %d 筆）" % audit_retention.BATCH_SIZE \
                    if result.found >= audit_retention.BATCH_SIZE else ""
                print(f"超過 {days} 天的紀錄：{result.found} 筆{more}。沒有動任何東西。")
                return
            if not run_all or result.found == 0 or result.trashed == 0:
                break
    finally:
        await notion_api.close_client()
    print(
        f"找到 {total.found} 筆，存進 CSV {total.archived} 筆，移到垃圾桶 {total.trashed} 筆，"
        f"失敗 {total.failed} 筆。CSV 在 {archive_dir}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="只看有幾筆，不動任何東西")
    parser.add_argument("--all", action="store_true", help="一直清到沒有為止")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run, args.all))
