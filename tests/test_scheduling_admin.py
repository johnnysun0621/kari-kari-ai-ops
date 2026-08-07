from datetime import datetime
from types import SimpleNamespace

import config
from cogs import scheduling_admin


def test_duration_candidates_use_requested_duration_and_30_minute_steps():
    start = datetime(2026, 8, 13, 13, 30, tzinfo=config.TZ)
    end = datetime(2026, 8, 13, 18, 0, tzinfo=config.TZ)

    candidates = scheduling_admin._duration_candidates([(start, end)], 60)

    assert candidates[0] == (start, datetime(2026, 8, 13, 14, 30, tzinfo=config.TZ))
    assert candidates[-1] == (
        datetime(2026, 8, 13, 17, 0, tzinfo=config.TZ),
        datetime(2026, 8, 13, 18, 0, tzinfo=config.TZ),
    )
    assert len(candidates) == 8


def test_duration_candidates_do_not_truncate_candidates_by_discord_page_size():
    start = datetime(2026, 8, 1, 8, 0, tzinfo=config.TZ)
    end = datetime(2026, 8, 5, 18, 0, tzinfo=config.TZ)

    candidates = scheduling_admin._duration_candidates([(start, end)], 60)

    assert len(candidates) > scheduling_admin.CANDIDATE_PAGE_SIZE
    assert candidates[-1][1] == datetime(2026, 8, 5, 18, 0, tzinfo=config.TZ)


def test_candidate_result_is_grouped_by_date():
    candidates = [
        (datetime(2026, 8, 13, 13, 30, tzinfo=config.TZ), datetime(2026, 8, 13, 14, 30, tzinfo=config.TZ)),
        (datetime(2026, 8, 13, 14, 0, tzinfo=config.TZ), datetime(2026, 8, 13, 15, 0, tzinfo=config.TZ)),
        (datetime(2026, 8, 14, 13, 30, tzinfo=config.TZ), datetime(2026, 8, 14, 14, 30, tzinfo=config.TZ)),
    ]

    lines = scheduling_admin._format_candidate_groups(candidates)

    assert lines == [
        "**8/13（週四）**",
        "　13:30–14:30　14:00–15:00",
        "**8/14（週五）**",
        "　13:30–14:30",
    ]


def test_candidate_flow_views_expose_back_navigation():
    operator = SimpleNamespace(id=123)
    artist = SimpleNamespace(page_id="artist-1", name="Johnny", status="已簽約")
    start = datetime(2026, 8, 13, 13, 30, tzinfo=config.TZ)
    end = datetime(2026, 8, 13, 14, 30, tzinfo=config.TZ)

    candidate_view = scheduling_admin.MonthlyPoolCandidateSelectView(
        "2026-08", 60, [artist], [(start, end)], operator,
    )
    assert any(getattr(child, "label", "") == "返回藝人選擇" for child in candidate_view.children)

    task_view = scheduling_admin.TaskTypeView(
        "2026-08", 60, [artist], [(start, end)], operator, ["通告", "課程與作業"],
    )
    assert any(getattr(child, "label", "") == "返回候選時段" for child in task_view.children)


def test_course_teacher_view_accepts_back_navigation_context():
    operator = SimpleNamespace(id=123)
    artist = SimpleNamespace(page_id="artist-1", name="Johnny", status="已簽約")
    project = SimpleNamespace(page_id="project-1", name="2026 年 8 月通識課")

    view = scheduling_admin.TeacherSelectView(
        "2026-08", 60, [artist], [], operator, "歌唱課",
        [("teacher-1", "老師")], "project-1", [project], ["課程與作業"], [artist],
    )

    assert any(getattr(child, "label", "") == "返回 Project" for child in view.children)
