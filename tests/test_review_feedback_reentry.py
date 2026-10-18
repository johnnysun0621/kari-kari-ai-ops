"""老師切去 Drive 上傳回來、ephemeral 訊息消失後，要能找回「貼 Drive 連結補回饋／完成回饋」。

已審過的提交不會再出現在 /審核提交，所以這是唯一的回頭路（2026-10-18 QA）。
"""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import config
from cogs import submissions
from services import repositories

NOW = datetime.now(config.TZ)


class FakeChannel:
    def __init__(self):
        self.id = 7
        self.sent: list[tuple[str, object]] = []

    async def send(self, content="", view=None, **kwargs):
        self.sent.append((content, view))


def make_cog_with_pending():
    cog = submissions.SubmissionsCog.__new__(submissions.SubmissionsCog)
    cog.pending_file_submissions = {}
    submission = repositories.SubmissionItem(page_id="s1", name="編曲作業提交", status="需修改")
    pending = submissions.PendingReviewFeedbackFile(
        submission=submission, result="需修改", feedback="副歌再收", reviewer="老師",
        channel_id=7, created_at=NOW, expires_at=NOW + timedelta(hours=3),
    )
    cog.pending_review_feedback_files = {42: pending}
    return cog, pending


def message(text="好了", attachments=()):
    return SimpleNamespace(
        author=SimpleNamespace(id=42), content=text, attachments=list(attachments), channel=FakeChannel(),
    )


def test_回到頻道打字就重新給按鈕():
    cog, pending = make_cog_with_pending()
    msg = message()
    msg.channel.id = 7

    asyncio.run(cog._dispatch_pending_message(msg))

    content, view = msg.channel.sent[-1]
    assert "編曲作業提交" in content
    assert isinstance(view, submissions.ReviewFeedbackLargeFileChoiceView)
    labels = [getattr(item, "label", "") for item in view.children]
    assert "貼 Drive 連結補回饋" in labels and "完成回饋" in labels
    assert view.owner_id == 42


def test_重新給的按鈕別人按不動():
    cog, pending = make_cog_with_pending()
    view = cog._review_feedback_controls(pending, 42)
    replies = []

    async def send_message(content, **kwargs):
        replies.append(content)

    async def send_modal(modal):
        raise AssertionError("別人不該打開回饋表單")

    other = SimpleNamespace(
        user=SimpleNamespace(id=99),
        response=SimpleNamespace(send_message=send_message, send_modal=send_modal),
    )
    button = next(item for item in view.children if getattr(item, "label", "") == "貼 Drive 連結補回饋")

    asyncio.run(button.callback(other))

    assert replies and "只給原本審核的人" in replies[0]


def test_交件清單的返回我的任務按得動():
    calls = []

    class Cog:
        async def return_to_my_tasks(self, interaction):
            calls.append(interaction)

    view = submissions.SubmitTaskView(Cog(), [], NOW)
    button = next(item for item in view.children if getattr(item, "label", "") == "返回我的任務")

    asyncio.run(button.callback("interaction"))

    assert calls == ["interaction"]
