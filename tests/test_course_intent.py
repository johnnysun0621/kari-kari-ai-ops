import asyncio
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


def test_colloquial_month_course_list_is_broad_query():
    assert course_intent.is_broad_query("我八月上了啥課")


def test_explicit_course_month_in_title_overrides_record_date():
    item = repositories.CourseMeetingRecord(
        page_id="course",
        url="https://notion.so/course",
        title="測試用｜2026-08-03",
        date=datetime(2026, 7, 22, tzinfo=config.TZ),
        summary="課程整理",
        content="課程正文",
        artist_page_ids=("artist-1",),
        task_page_ids=(),
        project_page_ids=(),
    )

    assert course_intent.record_months(item) == {"2026-08"}
    assert course_intent._fallback_intent("我八月上的課", [item]).record_ids == ("course",)


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


def test_open_ended_question_uses_bounded_records_for_evidence_check():
    records = [record("july", "歌唱課", 7), record("august", "表演課", 8)]

    intent = course_intent._fallback_intent("我想問薪資制度", records)

    assert course_intent.select_records(records, intent) == records


def test_note_summary_followup_reuses_previous_course_scope_without_llm():
    records = [record("july", "歌唱課", 7), record("august", "表演課", 8)]

    intent = asyncio.run(course_intent.classify_query(
        "幫我整理筆記",
        records,
        memory={"month": "2026-08", "record_ids": ["august"]},
    ))

    assert intent.mode == "summarize"
    assert intent.month == "2026-08"
    assert intent.record_ids == ("august",)


def test_broad_question_can_select_bounded_catalog():
    records = [record(str(index), f"第{index}堂課", 8) for index in range(12)]

    intent = course_intent._fallback_intent("我想統整上過哪些課", records)
    selected = course_intent.select_records(records, intent)

    assert len(selected) == course_intent.MAX_SELECTED_RECORDS
    assert [item.page_id for item in selected] == [str(index) for index in range(8)]
