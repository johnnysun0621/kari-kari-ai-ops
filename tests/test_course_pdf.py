from services.repositories import _file_block_url


def test_file_block_url_supports_notion_hosted_file():
    assert _file_block_url({"type": "file", "file": {"url": "https://signed.example/a.pdf"}}) == "https://signed.example/a.pdf"


def test_file_block_url_supports_external_file_shapes():
    assert _file_block_url({"type": "file", "external": {"url": "https://drive.example/a.pdf"}}) == "https://drive.example/a.pdf"
    assert _file_block_url({"type": "file", "file": {"external": {"url": "https://drive.example/b.pdf"}}}) == "https://drive.example/b.pdf"
