from services import arrangement


def test_only_course_requires_teacher_confirmation():
    assert arrangement.requires_teacher_confirmation("課程") is True
    assert arrangement.requires_teacher_confirmation("錄音") is False
    assert arrangement.requires_teacher_confirmation("通告") is False
    assert arrangement.requires_teacher_confirmation("其他") is False


def test_recording_and_notice_publish_immediately():
    assert arrangement.publishes_immediately("錄音") is True
    assert arrangement.publishes_immediately("通告") is True
    assert arrangement.publishes_immediately("其他") is False


def test_non_course_arrangements_use_existing_task_types():
    assert arrangement.task_type_for_arrangement("錄音") == "錄音"
    assert arrangement.task_type_for_arrangement("通告") == "演出"
    assert arrangement.task_type_for_arrangement("其他") == "行政"
    assert arrangement.task_type_for_arrangement("課程") is None
