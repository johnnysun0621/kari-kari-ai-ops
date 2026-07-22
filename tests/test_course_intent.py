from datetime import datetime

import config
from services import course_intent, repositories


def record(page_id: str, title: str, month: int) -> repositories.CourseMeetingRecord:
    return repositories.CourseMeetingRecord(
        page_id=page_id,
        url=f"https://notion.so/{page_id}",
        title=title,
        date=datetime(2026, month, 5, tzinfo=config.TZ),
        summary="氣息與作業",
        content="課程正文",
        artist_page_ids=("artist-1",),
        task_page_ids=(),
        project_page_ids=(),
    )


def test_month_query_selects_only_matching_records():
    records = [record("july", "歌唱課", 7), record("august", "表演課", 8)]

    intent = course_intent._fallback_intent("我想知道 7 月上了什麼課", records)

    assert intent.mode == "month_overview"
    assert [item.page_id for item in course_intent.select_records(records, intent)] == ["july"]


def test_follow_up_uses_previous_record_context_when_query_has_no_filter():
    records = [record("july", "歌唱課", 7), record("august", "表演課", 8)]
    intent = course_intent.QueryIntent()

    selected = course_intent.select_records(
        records, intent, memory={"record_ids": ["august"]}
    )

    assert [item.page_id for item in selected] == ["august"]


def test_other_follow_up_excludes_previous_course():
    records = [record("july", "歌唱課", 7), record("august", "表演課", 8)]
    assert course_intent._is_other_followup(
        "你再看看其他的", {"record_ids": ["july"]}
    )

    intent = course_intent.QueryIntent(mode="compare")
    selected = course_intent.select_records(
        records, intent, memory={"record_ids": ["july"]}
    )

    assert [item.page_id for item in selected] == ["august"]
