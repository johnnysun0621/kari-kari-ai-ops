from datetime import datetime

import config
from services import operations_qa, repositories


def test_context_contains_tasks_projects_meetings_and_review_feedback():
    task = repositories.TaskItem(
        page_id="task-1",
        name="八月歌唱課",
        status="進行中",
        task_type="課程與作業",
        description="練習副歌音準",
        end=datetime(2026, 8, 20, 18, 0, tzinfo=config.TZ),
    )
    project = repositories.ProjectItem(
        page_id="project-1",
        name="2026 八月課程培訓",
        status="進行中",
        project_type="課程培訓",
    )
    meeting = repositories.CourseMeetingRecord(
        page_id="meeting-1",
        url="https://notion.so/meeting",
        title="八月歌唱課會議",
        date=datetime(2026, 8, 5, tzinfo=config.TZ),
        summary="決議下週加強和聲",
        content="",
        artist_page_ids=(),
        task_page_ids=(),
        project_page_ids=(),
    )
    submission = repositories.SubmissionItem(
        page_id="submission-1",
        name="Johnny｜歌唱作業",
        status="需修改",
        review_feedback="老師回饋：尾音需要更穩定",
    )

    text = operations_qa.build_operations_context(
        [task], [project], [meeting], [submission]
    )

    assert "八月歌唱課" in text
    assert "2026 八月課程培訓" in text
    assert "決議下週加強和聲" in text
    assert "尾音需要更穩定" in text


def test_context_does_not_include_submission_without_feedback():
    submission = repositories.SubmissionItem(
        page_id="submission-1",
        name="Johnny｜歌唱作業",
        status="待審核",
        note="藝人的私人提交全文",
    )

    text = operations_qa.build_operations_context([], [], [], [submission])

    assert "私人提交全文" not in text


def test_large_task_list_does_not_crowd_out_meetings_and_feedback():
    tasks = [
        repositories.TaskItem(
            page_id=f"task-{index}",
            name=f"任務 {index}",
            status="進行中",
            task_type="企劃",
            description="很長的任務說明" * 100,
        )
        for index in range(100)
    ]
    meeting = repositories.CourseMeetingRecord(
        page_id="meeting-1",
        url="",
        title="重要決策會議",
        date=None,
        summary="最後決定九月發布",
        content="",
        artist_page_ids=(),
        task_page_ids=(),
        project_page_ids=(),
    )
    submission = repositories.SubmissionItem(
        page_id="submission-1",
        name="歌唱作業",
        status="通過",
        review_feedback="老師確認可以結案",
    )

    text = operations_qa.build_operations_context(tasks, [], [meeting], [submission])

    assert "最後決定九月發布" in text
    assert "老師確認可以結案" in text
