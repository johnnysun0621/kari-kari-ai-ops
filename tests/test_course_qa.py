from cogs.course_qa import _sanitize_memory, _split_discord_message, can_artist_use_ai
from services import course_qa, llm_usage, repositories


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


def test_persisted_memory_drops_legacy_question_text():
    memory = _sanitize_memory({
        "last_question": "這段不應繼續保存",
        "mode": "course_question",
        "record_ids": ["meeting-1"],
    })

    assert "last_question" not in memory
    assert memory["record_ids"] == ["meeting-1"]
