from datetime import datetime

import config
from cogs import ui_text
from services.repositories import TaskItem


def test_needs_fix_displays_revision_deadline_instead_of_original_submission_deadline():
    task = TaskItem(
        page_id="task-1",
        name="歌唱作業",
        status="需修改",
        task_type="課程與作業",
        open_for_submission=True,
        submission_deadline=datetime(2026, 8, 10, 23, 59, tzinfo=config.TZ),
        submission_deadline_has_time=True,
        revision_deadline=datetime(2026, 8, 18, 12, 0, tzinfo=config.TZ),
    )

    text = ui_text.deadline_text(
        task,
        datetime(2026, 8, 16, 10, 0, tzinfo=config.TZ),
    )

    assert text == "修改期限 8/18 12:00（剩 2 天）"
    assert "8/10" not in text
