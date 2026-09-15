from services import arrangement


def test_non_course_arrangements_create_unpublished_task_drafts():
    assert arrangement.creates_unpublished_task_draft("錄音") is True
    assert arrangement.creates_unpublished_task_draft("通告") is True
    assert arrangement.creates_unpublished_task_draft("其他") is True
    assert arrangement.creates_unpublished_task_draft("課程") is False


def test_non_course_arrangements_use_existing_task_types():
    assert arrangement.task_type_for_arrangement("錄音") == "錄音"
    assert arrangement.task_type_for_arrangement("通告") == "演出"
    assert arrangement.task_type_for_arrangement("其他") == "行政"
    assert arrangement.task_type_for_arrangement("課程") is None
