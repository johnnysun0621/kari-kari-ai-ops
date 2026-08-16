from datetime import datetime

import config
from cogs.course_qa import _sanitize_memory, _split_discord_message, can_artist_use_ai
from services import course_qa, llm_response, llm_usage, repositories


def test_split_discord_message_respects_limit_and_preserves_content():
    content = "第一段\n" + ("課程內容。" * 500) + "\n最後一段"

    chunks = _split_discord_message(content)

    assert len(chunks) > 1
    assert all(len(chunk) <= 1900 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == content.replace("\n", "")


def test_inactive_artist_cannot_use_course_ai():
    for status in ("未簽", "暫停", "封存"):
        artist = repositories.Artist("artist-1", "Johnny", "123", status=status)
        assert can_artist_use_ai(artist) is False


def test_signed_and_negotiating_artist_can_use_course_ai():
    for status in ("已簽約", "洽談中", None):
        artist = repositories.Artist("artist-1", "Johnny", "123", status=status)
        assert can_artist_use_ai(artist) is True


def test_quota_message_names_artist_limit():
    status = llm_usage.LimitStatus(
        allowed=False,
        system_remaining=42000,
        artist_remaining=0,
    )

    assert "個人每日額度" in course_qa.quota_message(status)


def test_quota_message_names_system_limit():
    status = llm_usage.LimitStatus(
        allowed=False,
        system_remaining=0,
        artist_remaining=1000,
    )

    assert "全系統今日額度" in course_qa.quota_message(status)


def test_course_assistant_skill_is_loaded():
    skill = course_qa.load_course_assistant_skill()

    assert "檢索優先順序" in skill
    assert "Date" in skill


def test_month_overview_is_generated_without_llm_and_includes_sources():
    records = [
        repositories.CourseMeetingRecord(
            page_id="august-1",
            url="https://notion.so/august-1",
            title="八月歌唱課",
            date=datetime(2026, 8, 6, tzinfo=config.TZ),
            summary="練習呼吸與節奏",
            content="完整課程內容",
            artist_page_ids=("artist-1",),
            task_page_ids=(),
            project_page_ids=(),
        ),
        repositories.CourseMeetingRecord(
            page_id="august-2",
            url="https://notion.so/august-2",
            title="八月表演課",
            date=datetime(2026, 8, 3, tzinfo=config.TZ),
            summary="",
            content="舞台走位與表情管理\n第二段內容",
            artist_page_ids=("artist-1",),
            task_page_ids=(),
            project_page_ids=(),
        ),
    ]

    answer = course_qa.build_month_overview_answer(records, "2026-08")

    assert "2026 年 8 月共有 2 筆課程紀錄" in answer
    assert "8/3｜八月表演課" in answer
    assert "舞台走位與表情管理" in answer
    assert "8/6｜八月歌唱課" in answer
    assert "練習呼吸與節奏" in answer
    assert "[八月歌唱課](https://notion.so/august-1)" in answer


def test_persisted_memory_drops_legacy_question_text():
    memory = _sanitize_memory({
        "last_question": "這段不應繼續保存",
        "mode": "course_question",
        "record_ids": ["meeting-1"],
    })

    assert "last_question" not in memory
    assert memory["record_ids"] == ["meeting-1"]


def test_context_builder_keeps_record_boundaries_and_respects_limit():
    records = [
        repositories.CourseMeetingRecord(
            page_id=f"record-{index}",
            url=f"https://notion.so/record-{index}",
            title=f"課程 {index}",
            date=None,
            summary="摘要",
            content="內容 " + ("x" * 300),
            artist_page_ids=("artist-1",),
            task_page_ids=(),
            project_page_ids=(),
        )
        for index in range(10)
    ]

    context = course_qa.build_context(records, max_chars=900)

    assert len(context) <= 900
    assert context.startswith("【課程 0")
    assert "來源：https://notion.so/record-0" in context
    if "【課程 1" in context:
        assert "來源：https://notion.so/record-1" in context


def test_extract_llm_text_reads_openai_message_content():
    payload = {"choices": [{"message": {"content": "完整答案"}}]}

    assert llm_response.extract_text(payload) == "完整答案"


def test_extract_llm_text_reads_content_block_list():
    payload = {
        "choices": [
            {"message": {"content": [{"type": "text", "text": "分段答案"}]}}
        ]
    }

    assert llm_response.extract_text(payload) == "分段答案"


def test_extract_llm_text_reads_common_compatible_fallbacks():
    assert llm_response.extract_text({"choices": [{"text": "舊式答案"}]}) == "舊式答案"
    assert llm_response.extract_text({"output_text": "輸出答案"}) == "輸出答案"


def test_extract_llm_text_returns_empty_for_incomplete_response():
    assert llm_response.extract_text({"choices": [{"message": {"content": None}}]}) == ""
