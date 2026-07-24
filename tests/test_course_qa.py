from cogs.course_qa import _split_discord_message


def test_split_discord_message_respects_limit_and_preserves_content():
    content = "第一段\n" + ("課程內容。" * 500) + "\n最後一段"

    chunks = _split_discord_message(content)

    assert len(chunks) > 1
    assert all(len(chunk) <= 1900 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == content.replace("\n", "")
