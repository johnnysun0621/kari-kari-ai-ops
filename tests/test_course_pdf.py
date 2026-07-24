from services.course_qa import _record_text
from services.repositories import CourseAttachment, CourseMeetingRecord, _extract_file_text, _file_block_url, _file_extension


def test_file_block_url_supports_notion_hosted_file():
    assert _file_block_url({"type": "file", "file": {"url": "https://signed.example/a.pdf"}}) == "https://signed.example/a.pdf"


def test_file_block_url_supports_external_file_shapes():
    assert _file_block_url({"type": "file", "external": {"url": "https://drive.example/a.pdf"}}) == "https://drive.example/a.pdf"
    assert _file_block_url({"type": "file", "file": {"external": {"url": "https://drive.example/b.pdf"}}}) == "https://drive.example/b.pdf"


def test_file_block_url_supports_nested_notion_file_shape():
    block = {"type": "file", "file": {"name": "講義.pdf", "file": {"url": "https://signed.example/c.pdf"}}}
    assert _file_block_url(block) == "https://signed.example/c.pdf"


def test_file_extension_uses_name_or_content_type():
    block = {"type": "file", "file": {"name": "課程講義.PPTX"}}
    assert _file_extension(block, "https://signed.example/file") == ".pptx"
    assert _file_extension({"type": "file", "file": {}}, "https://signed.example/file", "application/pdf") == ".pdf"


def test_plain_text_file_extraction():
    assert _extract_file_text("課程重點".encode(), ".txt") == "課程重點"


def test_attachment_text_is_not_sent_without_explicit_selection():
    record = CourseMeetingRecord(
        page_id="page",
        url="https://notion.so/page",
        title="課程",
        date=None,
        summary="頁面摘要",
        content="頁面正文",
        artist_page_ids=(),
        task_page_ids=(),
        project_page_ids=(),
        attachments=(CourseAttachment("講義.pdf", ".pdf", "附件正文"),),
    )

    assert "附件正文" not in _record_text(record)
    assert "附件正文" in _record_text(record, ("講義.pdf",))
