"""Read-only Notion smoke test for finalized KariKari data sources."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from services import notion_api


DATA_SOURCES = {
    "Artists": lambda tenant: tenant.ds_artists,
    "Availability Slots": lambda tenant: tenant.ds_availability_slots,
    "Tasks": lambda tenant: tenant.ds_tasks,
    "Projects": lambda tenant: tenant.ds_projects,
    "People": lambda tenant: tenant.ds_people,
    "Submissions": lambda tenant: tenant.ds_submissions,
    "Bulletin": lambda tenant: tenant.ds_bulletin,
    "Course Meetings": lambda tenant: tenant.ds_course_meetings,
    "Audit Log": lambda tenant: tenant.ds_audit_log,
}


async def main() -> int:
    tenant = config.get_tenant()
    if not tenant.notion_token:
        print("ERROR: NOTION_TOKEN is not configured")
        return 1
    failed = False
    for name, data_source in DATA_SOURCES.items():
        try:
            pages = await notion_api.query_data_source(data_source(tenant))
        except Exception as exc:
            failed = True
            print(f"FAIL {name}: {type(exc).__name__}")
        else:
            print(f"OK   {name}: readable ({len(pages)} pages)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
