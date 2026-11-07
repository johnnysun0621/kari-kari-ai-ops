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


class FakeResponse:
    def __init__(self, log):
        self.log = log

    async def send_message(self, content, **kwargs):
        self.log.append(("send", content, kwargs))

    async def edit_message(self, **kwargs):
        self.log.append(("edit", kwargs.get("content"), kwargs))

    async def send_modal(self, modal):
        self.log.append(("modal", modal, {}))

    async def defer(self, **kwargs):
        self.log.append(("defer", None, kwargs))


class FakeMessage:
    def __init__(self, log, content="✅ 已收到 1 個批改檔\n審核對象：**編曲作業提交**（需修改）"):
        self.log = log
        self.content = content

    async def edit(self, **kwargs):
        self.log.append(("message_edit", None, kwargs))


def press(cog, action, submission_id="s1", user_id=42):
    """照 Discord 實際的流程：從 custom_id 重建按鈕再呼叫，等於 bot 重開後按舊訊息。"""
    log = []

    async def followup_send(content, **kwargs):
        log.append(("followup", content, kwargs))

    interaction = SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        client=SimpleNamespace(get_cog=lambda name: cog),
        message=FakeMessage(log),
        response=FakeResponse(log),
        followup=SimpleNamespace(send=followup_send),
    )
    custom_id = f"review_feedback:{action}:42:{submission_id}"
    match = submissions.ReviewFeedbackButton.__discord_ui_compiled_template__.fullmatch(custom_id)
    button = asyncio.run(submissions.ReviewFeedbackButton.from_custom_id(interaction, None, match))
    asyncio.run(button.callback(interaction))
    return log


def test_bot重開後按舊訊息_有回應並拿掉按鈕():
    cog, _ = make_cog_with_pending()
    cog.pending_review_feedback_files = {}  # 重開後記憶體是空的
    for action in submissions.REVIEW_FEEDBACK_ACTIONS:
        log = press(cog, action)
        kind, content, kwargs = log[0]
        assert kind == "edit" and kwargs["view"] is None
        assert "入口已經關閉" in content and "審核結果已經存好" in content
        assert "編曲作業提交" in content, "原本的審核對象要留著"


def test_入口過期_清掉並說明():
    cog, pending = make_cog_with_pending()
    pending.expires_at = NOW - timedelta(minutes=1)
    log = press(cog, "drive")
    assert log[0][0] == "edit" and "入口已經關閉" in log[0][1]
    assert cog.pending_review_feedback_files == {}


def test_舊訊息不能動到正在審的另一筆():
    cog, pending = make_cog_with_pending()
    pending.attachments.append(object())
    log = press(cog, "finish", submission_id="old-submission")
    assert log[0][0] == "edit" and "入口已經關閉" in log[0][1]
    assert cog.pending_review_feedback_files == {42: pending}


def test_還開著_貼連結打開表單():
    cog, pending = make_cog_with_pending()
    log = press(cog, "drive")
    assert log[0][0] == "modal"
    assert isinstance(log[0][1], submissions.ReviewFeedbackDriveLinkModal)
    assert log[0][1].submission is pending.submission


def test_還開著_完成回饋_拿掉按鈕並歸檔():
    cog, pending = make_cog_with_pending()
    pending.attachments.append(object())
    archived = []

    async def archive(*, pending, discord_user_id):
        archived.append((pending, discord_user_id))
        return True, "✅ 已歸檔"

    cog._archive_review_feedback_files = archive
    log = press(cog, "finish")
    assert archived == [(pending, 42)]
    assert cog.pending_review_feedback_files == {}
    assert ("message_edit", None, {"view": None}) in log
    assert log[-1][1] == "✅ 已歸檔"


def test_還開著_沒傳檔就按完成_請他先傳_入口不關():
    cog, pending = make_cog_with_pending()
    log = press(cog, "finish")
    assert log[0][0] == "send" and "請先上傳" in log[0][1]
    assert cog.pending_review_feedback_files == {42: pending}


def test_還開著_返回選擇_公開訊息收掉_選擇只給本人():
    cog, pending = make_cog_with_pending()
    log = press(cog, "back")
    assert log[0][0] == "edit" and log[0][2]["view"] is None and "編曲作業提交" in log[0][1]
    kind, content, kwargs = log[1]
    assert kind == "followup" and kwargs["ephemeral"] is True and "編曲作業提交" in content
    assert isinstance(kwargs["view"], submissions.PostReviewAttachmentView)
    assert cog.pending_review_feedback_files == {}


def test_收到批改檔的訊息_寫出在審哪一筆(monkeypatch):
    cog, pending = make_cog_with_pending()
    monkeypatch.setattr(submissions.drive_api, "is_configured", lambda: True)
    msg = message(attachments=[SimpleNamespace(filename="a.pdf", size=100)])
    asyncio.run(cog._dispatch_pending_message(msg))
    content, view = msg.channel.sent[-1]
    assert "審核對象：**編曲作業提交**（需修改）" in content
    assert view.is_persistent(), "按鈕不能在 bot 重開或過期後變成按了沒反應"


def test_custom_id_沒超過_Discord_上限():
    long_id = "3fdc6f59-14c2-8163-a0e9-e603ff57a326"
    for action in submissions.REVIEW_FEEDBACK_ACTIONS:
        button = submissions.ReviewFeedbackButton(action, 1366089780877725838, long_id)
        assert len(button.custom_id) <= 100


def test_交件清單的返回我的任務按得動():
    calls = []

    class Cog:
        async def return_to_my_tasks(self, interaction):
            calls.append(interaction)

    view = submissions.SubmitTaskView(Cog(), [], NOW)
    button = next(item for item in view.children if getattr(item, "label", "") == "返回我的任務")

    asyncio.run(button.callback("interaction"))

    assert calls == ["interaction"]
