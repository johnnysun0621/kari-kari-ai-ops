"""QA 用：把正式環境每一筆任務的分類與逾期判定印出來，給 PM 對照 Notion 畫面。

純讀取，不寫任何東西。給規則 M2-01（專案類型分類）與 M5-03（課程任務不判逾期）
做 QA 用——那兩項是純函式，Discord 上沒有任何可以點的介面，所以用這支列印實際
判斷結果，由 PM 逐列和 Notion 上看到的對照。

用法：

    .venv/Scripts/python.exe scripts/qa_classification.py            # 摘要
    .venv/Scripts/python.exe scripts/qa_classification.py --all      # 逐筆列出
    .venv/Scripts/python.exe scripts/qa_classification.py --type 課程培訓
"""
import argparse
import asyncio
import io
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from services import notion_api, repositories


def _utf8_stdout() -> None:
    """Windows 主控台預設 CP950，中文會變亂碼。"""
    if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
        )


def _overdue_verdict(task: repositories.TaskItem, now: datetime) -> str:
    """這一刻系統對這筆任務的逾期判定，連同原因。"""
    if task.status in repositories.INACTIVE_TASK_STATUSES:
        return "終態，不判"
    if task.status == "待審核":
        return "待審核，停止判定"
    deadline, _ = repositories.task_action_deadline(task)
    if not deadline:
        return "沒有追蹤期限"
    if not repositories.task_deadline_passed(task, now):
        return "期限未到"
    if repositories.task_skips_overdue(task):
        return "過期但豁免（課程培訓）"
    return "判逾期"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="逐筆列出，不只印摘要")
    parser.add_argument("--type", default=None, help="只看某個專案類型")
    args = parser.parse_args()

    _utf8_stdout()
    tenant = config.get_tenant()
    if not tenant.notion_token:
        print("ERROR: NOTION_TOKEN 沒有設定")
        return 1

    now = datetime.now(config.TZ)
    pages = await notion_api.query_data_source(tenant.ds_tasks)
    tasks = [repositories._to_task(page) for page in pages]
    await notion_api.close_client()

    print(f"基準時間 {now:%Y-%m-%d %H:%M}（台北）｜任務 {len(tasks)} 筆\n")

    print("── 專案類型分布（M2-01）" + "─" * 32)
    dist = Counter(
        repositories.task_project_type(t) or "（沒有分類）" for t in tasks
    )
    for name, count in dist.most_common():
        exempt = "　← 不判逾期" if name in repositories.NO_OVERDUE_PROJECT_TYPES else ""
        print(f"  {count:4}  {name}{exempt}")

    multi = [t for t in tasks if len(t.project_types) > 1]
    if multi:
        print(f"\n  關聯到多個類型的任務 {len(multi)} 筆（task_project_type 回 None）：")
        for t in multi:
            print(f"    - {t.name[:40]}  {t.project_types}")

    print("\n── 逾期判定（M5-03）" + "─" * 36)
    for verdict, count in Counter(_overdue_verdict(t, now) for t in tasks).most_common():
        print(f"  {count:4}  {verdict}")

    exempted = [t for t in tasks if _overdue_verdict(t, now) == "過期但豁免（課程培訓）"]
    print(f"\n  被豁免的任務 {len(exempted)} 筆"
          + ("：" if exempted else "（目前沒有課程任務過期）"))
    for t in exempted:
        deadline, _ = repositories.task_action_deadline(t)
        print(f"    - {t.name[:40]}  期限 {deadline:%Y-%m-%d}  狀態 {t.status}")

    if args.all or args.type:
        print("\n── 逐筆" + "─" * 49)
        shown = [
            t for t in tasks
            if not args.type or args.type in t.project_types
        ]
        print(f"（{len(shown)} 筆）\n")
        for t in sorted(shown, key=lambda x: x.name):
            deadline, _ = repositories.task_action_deadline(t)
            deadline_text = f"{deadline:%m/%d}" if deadline else "—"
            print(
                f"  {t.name[:34]:36}"
                f" 分類={repositories.task_project_type(t) or '—':12}"
                f" 狀態={t.status or '—':6}"
                f" 期限={deadline_text:6}"
                f" 逾期判定={_overdue_verdict(t, now)}"
            )

    print("\n要對照的是：這裡印出的「分類」要和 Notion 上該任務的「專案類型」欄位一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
