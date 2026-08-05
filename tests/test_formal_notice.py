from datetime import datetime

import config
from services import formal_notice, repositories


def _task(**overrides):
    values = dict(
        page_id="task-1",
        name="八月錄音",
        status="尚未開始",
        task_type="錄音",
        artist_page_ids=["artist-1"],
        start=datetime(2026, 8, 5, 14, 0, tzinfo=config.TZ),
        end=datetime(2026, 8, 5, 16, 0, tzinfo=config.TZ),
        start_has_time=True,
        end_has_time=True,
    )
    values.update(overrides)
    return repositories.TaskItem(**values)


def test_publishable_task_requires_precise_hard_blocking_time():
    assert repositories.is_publishable_formal_task(_task())
    assert not repositories.is_publishable_formal_task(
        _task(task_type="行政")
    )
    assert not repositories.is_publishable_formal_task(
        _task(start_has_time=False, end_has_time=False)
    )


def test_course_task_is_publishable_when_course_category_exists():
    assert repositories.is_publishable_formal_task(
        _task(task_type="課程與作業", course_category="歌唱課")
    )


def test_description_field_extracts_location():
    description = "安排來源：/任務建立與安排\n地點／連結：https://example.com\n內容：錄音"

    assert repositories.task_description_field(
        description, "地點", "地點／連結"
    ) == "https://example.com"


def test_task_draft_writes_separate_start_end_and_stays_hidden():
    start = datetime(2026, 8, 5, 14, 0, tzinfo=config.TZ)
    end = datetime(2026, 8, 5, 16, 0, tzinfo=config.TZ)
    props = repositories._build_formal_arrangement_task_props(
        name="八月錄音",
        task_type="錄音",
        artist_page_ids=["artist-1"],
        description="草稿",
        start=start,
        end=end,
        course_category=None,
        teacher_page_ids=None,
        project_page_ids=None,
        discord_visible=False,
    )

    assert props["Discord 顯示"]["checkbox"] is False
    assert props["開始日"]["date"]["start"] == start.isoformat()
    assert "end" not in props["開始日"]["date"]
    assert props["截止日"]["date"]["start"] == end.isoformat()


def test_formal_notice_state_prevents_duplicate_after_reload(tmp_path):
    path = tmp_path / "formal_notices.json"
    formal_notice.reset_for_tests(path)

    assert not formal_notice.was_published("task-1")
    formal_notice.mark_published("task-1")
    assert formal_notice.was_published("task-1")

    formal_notice.reset_for_tests(path)
    assert formal_notice.was_published("task-1")
