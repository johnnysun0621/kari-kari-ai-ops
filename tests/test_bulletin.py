from datetime import datetime

import config
from cogs.bulletin import bulletin_fingerprint, format_bulletin
from services import notion_links, repositories
from services.repositories import BulletinItem


def _item(**changes):
    values = dict(
        page_id="page-1",
        url="https://notion.so/page-1",
        title="測試公告",
        category="📢 公告",
        status="未處理",
        date=datetime(2026, 7, 22, 10, 0, tzinfo=config.TZ),
        people=("Johnny",),
        pinned=False,
    )
    values.update(changes)
    return BulletinItem(**values)


def test_bulletin_fingerprint_changes_when_status_changes():
    assert bulletin_fingerprint(_item()) != bulletin_fingerprint(_item(status="已結案"))


def test_format_bulletin_contains_operational_fields_without_notion_link():
    text = format_bulletin(_item(pinned=True))
    assert "📌 **測試公告**" in text
    assert "類別：📢 公告｜狀態：未處理" in text
    assert "人員：Johnny" in text
    # 公告區藝人看得到；從任一 Notion 頁面都能往上翻到整個 workspace。
    assert not notion_links.contains_notion_link(text)


def test_ai_feedback_uses_usage_feedback_category_without_question_text():
    props, children = repositories.build_ai_feedback_page(
        artist_name="Johnny",
        discord_user="Johnny#1234",
        discord_user_id=1234,
        feedback="來源連結打不開",
        answer_message_url="https://discord.com/channels/1/2/3",
    )

    assert props["類別"]["select"]["name"] == "🙋 使用回饋"
    assert props["狀態"]["select"]["name"] == "未處理"
    body = children[0]["paragraph"]["rich_text"][0]["text"]["content"]
    assert "來源連結打不開" in body
    assert "原始問題" not in body
    assert "https://discord.com/channels/1/2/3" in body
