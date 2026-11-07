"""M4-21：附批改檔全程私密，訊息看得出在審哪一筆，按鈕不會變成按了沒反應。"""
import asyncio
from types import SimpleNamespace

import discord
import pytest

from cogs import submissions
from services import audit, drive_api, notion_api, repositories

SUBMISSION = repositories.SubmissionItem(
    page_id="3fdc6f59-14c2-8163-a0e9-e603ff57a326",
    name="方晨｜編曲作業｜10/18 14:09",
    status="需修改",
    artist_page_ids=["a1"],
    task_page_ids=["t1"],
    review_feedback="副歌再收",
    reviewer="老師｜42",
    url="https://notion.so/s",
)


class FakeInteraction:
    def __init__(self, user_id=42, message_content="原本的訊息", cog=None):
        self.log = []
        self.user = SimpleNamespace(id=user_id, send=self._dm)
        self.client = SimpleNamespace(get_cog=lambda name: cog)
        self.message = SimpleNamespace(content=message_content)
        self.dm_error = None
        log = self.log

        class Response:
            async def send_message(self, content, **kw):
                log.append(("send", content, kw))

            async def edit_message(self, **kw):
                log.append(("edit", kw.get("content"), kw))

            async def send_modal(self, modal):
                log.append(("modal", modal, {}))

            async def defer(self, **kw):
                log.append(("defer", None, kw))

        async def followup_send(content, **kw):
            log.append(("followup", content, kw))

        self.response = Response()
        self.followup = SimpleNamespace(send=followup_send)

    async def _dm(self, content, **kw):
        if self.dm_error:
            raise self.dm_error
        self.log.append(("dm", content, kw))

    async def edit_original_response(self, **kw):
        self.log.append(("edit_original", kw.get("content"), kw))


def attachment(name="a.pdf", size=100, content=b"x"):
    async def read():
        return content

    return SimpleNamespace(filename=name, size=size, read=read, content_type="application/pdf")


def post_review_view(cog=None):
    return submissions.PostReviewAttachmentView(
        cog or SimpleNamespace(), SUBMISSION, result="需修改", feedback="副歌再收", reviewer="老師｜42", reviewer_id=42,
    )


def button(view, label):
    return next(item for item in view.children if getattr(item, "label", "") == label)


def upload_modal(cog, files):
    modal = submissions.ReviewFeedbackUploadModal(
        cog, SUBMISSION, result="需修改", feedback="副歌再收", reviewer="老師｜42",
    )
    modal.files._values = files
    return modal


def test_每則都看得出任務和藝人():
    text = submissions.review_target_lines(SUBMISSION, "需修改")
    assert "任務：**編曲作業**" in text and "藝人：方晨" in text
    assert "交件時間：10/18 14:09" in text and "審核結果：需修改" in text
    detail = submissions.review_submission_detail_text(SUBMISSION)
    assert "任務：**編曲作業**" in detail


def test_任務名稱有直線也拆得對_舊格式不會壞():
    assert submissions.submission_name_parts("方晨｜A｜B｜10/18 14:09") == ("A｜B", "方晨", "10/18 14:09")
    assert submissions.submission_name_parts("方晨｜編曲") == ("編曲", "方晨", "")
    assert submissions.submission_name_parts("單一名稱") == ("單一名稱", "", "")


def test_審核清單任務放最前面():
    assert submissions.review_option_label(SUBMISSION) == "編曲作業（方晨）"
    assert "交件 10/18 14:09" in submissions.review_option_description(SUBMISSION)


def test_視窗裡真的有傳檔欄位():
    modal = upload_modal(SimpleNamespace(), [])
    assert modal.title == "附批改檔｜編曲作業"
    label = modal.to_components()[0]
    assert label["type"] == discord.ComponentType.label.value and "藝人：方晨" in label["description"]
    assert label["component"]["type"] == discord.ComponentType.file_upload.value
    assert label["component"]["min_values"] == 1 and label["component"]["max_values"] == 10


def test_按附批改檔_直接開視窗():
    interaction = FakeInteraction()
    asyncio.run(button(post_review_view(), "附批改檔").callback(interaction))
    assert interaction.log[0][0] == "modal"
    assert isinstance(interaction.log[0][1], submissions.ReviewFeedbackUploadModal)


@pytest.mark.parametrize("label", ["附批改檔", "檔案太大，用 Drive", "不需補批改檔"])
def test_別人按不動(label):
    interaction = FakeInteraction(user_id=99)
    asyncio.run(button(post_review_view(), label).callback(interaction))
    assert interaction.log[0][0] == "send" and "只給原本審核的人" in interaction.log[0][1]
    assert interaction.log[0][2]["ephemeral"] is True


def test_送出檔案_只改本人那則_成功後按鈕拿掉():
    calls = []

    class Cog:
        async def _archive_review_feedback_files(self, *, upload, discord_user_id):
            calls.append((upload, discord_user_id))
            return True, "✅ 已歸檔"

    files = [attachment()]
    interaction = FakeInteraction()
    asyncio.run(upload_modal(Cog(), files).on_submit(interaction))
    assert calls[0][0].attachments == files and calls[0][1] == 42
    assert [entry[0] for entry in interaction.log] == ["edit", "edit_original"]
    assert interaction.log[0][2]["view"] is None and "⏳" in interaction.log[0][1]
    assert "編曲作業" in interaction.log[1][1] and "✅ 已歸檔" in interaction.log[1][1]
    assert interaction.log[1][2]["view"] is None


def test_歸檔失敗_按鈕留著可以重試():
    class Cog:
        async def _archive_review_feedback_files(self, *, upload, discord_user_id):
            return False, "❌ 失敗"

    interaction = FakeInteraction()
    asyncio.run(upload_modal(Cog(), [attachment()]).on_submit(interaction))
    assert isinstance(interaction.log[-1][2]["view"], submissions.PostReviewAttachmentView)


def test_檔案太大_告訴他改走_Drive_不歸檔():
    class Cog:
        async def _archive_review_feedback_files(self, **kw):
            raise AssertionError("超過大小不該歸檔")

    big = attachment("big.mov", size=(submissions.MAX_UPLOAD_MB + 1) * 1024 * 1024)
    interaction = FakeInteraction()
    asyncio.run(upload_modal(Cog(), [big]).on_submit(interaction))
    kind, content, kw = interaction.log[0]
    assert kind == "send" and "big.mov" in content and "檔案太大，用 Drive" in content and kw["ephemeral"] is True


def make_cog():
    cog = submissions.SubmissionsCog.__new__(submissions.SubmissionsCog)
    cog.pending_file_submissions = {}
    cog.bot = None
    return cog


def test_歸檔_到存檔時才建資料夾(monkeypatch):
    cog = make_cog()
    built, uploaded, notified = [], [], []
    target = submissions.ReviewFeedbackDriveTarget(
        artist_page_id="a1", artist_name="方晨", folder_url="https://drive/f", base_folder_id="b", path_segments=["老師回饋"],
    )

    async def targets(submission, now):
        built.append(submission.page_id)
        return [target], []

    async def upload_file(**kw):
        uploaded.append(kw["filename"])
        return {"webViewLink": "https://drive/file"}

    async def update(*a, **kw):
        notified.append(("notion", kw["feedback"]))

    async def notify(submission, **kw):
        notified.append(("dm", kw["links_by_artist_page_id"]))
        return 1

    async def log_event(**kw):
        notified.append(("audit", kw["event_name"]))

    cog._review_feedback_drive_targets = targets
    cog._notify_review_feedback_file = notify
    monkeypatch.setattr(submissions.drive_api, "is_configured", lambda: True)
    monkeypatch.setattr(submissions.drive_api, "upload_submission_file", upload_file)
    monkeypatch.setattr(repositories, "update_submission_review", update)
    monkeypatch.setattr(audit, "log_event", log_event)
    upload = submissions.ReviewFeedbackUpload(
        submission=SUBMISSION, result="需修改", feedback="副歌再收", reviewer="老師｜42", attachments=[attachment()],
    )
    ok, text = asyncio.run(cog._archive_review_feedback_files(upload=upload, discord_user_id=42))
    assert ok and "1 個批改檔已歸檔" in text
    assert built == [SUBMISSION.page_id] and len(uploaded) == 1
    assert ("dm", {"a1": ["https://drive/file"]}) in notified


def test_歸檔_算不出資料夾_講真正原因(monkeypatch):
    cog = make_cog()

    async def targets(submission, now):
        return [], [submissions.NO_TASK_REASON]

    cog._review_feedback_drive_targets = targets
    monkeypatch.setattr(submissions.drive_api, "is_configured", lambda: True)
    upload = submissions.ReviewFeedbackUpload(
        submission=SUBMISSION, result="需修改", feedback="", reviewer="老師", attachments=[attachment()],
    )
    ok, text = asyncio.run(cog._archive_review_feedback_files(upload=upload, discord_user_id=42))
    assert not ok and "任務已經被刪除" in text


def test_在頻道傳檔_小律不回任何東西():
    cog = make_cog()
    sent = []

    async def send(*a, **kw):
        sent.append(a)

    message = SimpleNamespace(
        author=SimpleNamespace(id=42), attachments=[attachment()], channel=SimpleNamespace(id=7, send=send),
    )
    asyncio.run(cog._dispatch_pending_message(message))
    assert sent == []


def test_檔案太大_資料夾和按鈕用私訊(monkeypatch):
    cog = make_cog()
    monkeypatch.setattr(drive_api, "is_configured", lambda: False)
    interaction = FakeInteraction()
    asyncio.run(cog.start_review_drive_feedback(interaction, SUBMISSION, "需修改"))
    kind, content, kw = interaction.log[0]
    assert kind == "dm" and "任務：**編曲作業**" in content
    assert kw["view"].is_persistent(), "私訊會留很久，bot 重開後也要按得動"
    assert button(kw["view"], "貼 Drive 連結補回饋").submission_id == SUBMISSION.page_id
    assert interaction.log[1][0] == "followup" and interaction.log[1][2]["ephemeral"] is True


def test_私訊不通_改用只有本人看得到的訊息(monkeypatch):
    cog = make_cog()
    monkeypatch.setattr(drive_api, "is_configured", lambda: False)
    interaction = FakeInteraction()
    interaction.dm_error = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "cannot DM")
    asyncio.run(cog.start_review_drive_feedback(interaction, SUBMISSION, "需修改"))
    kind, content, kw = interaction.log[0]
    assert kind == "followup" and kw["ephemeral"] is True and kw["view"].is_persistent()


def test_私訊按鈕_bot重開後從custom_id還原():
    item = submissions.ReviewFeedbackDriveLinkButton(SUBMISSION.page_id)
    assert len(item.custom_id) <= 100
    match = submissions.ReviewFeedbackDriveLinkButton.__discord_ui_compiled_template__.fullmatch(item.custom_id)
    rebuilt = asyncio.run(submissions.ReviewFeedbackDriveLinkButton.from_custom_id(None, None, match))
    interaction = FakeInteraction()
    asyncio.run(rebuilt.callback(interaction))
    assert interaction.log[0][0] == "modal" and interaction.log[0][1].submission_id == SUBMISSION.page_id


def link_modal(link="https://drive.google.com/x", note=""):
    modal = submissions.ReviewFeedbackDriveLinkModal(SUBMISSION.page_id)
    modal.link_input._value = link
    modal.note_input._value = note
    return modal


def test_連結格式錯_不動任何東西():
    interaction = FakeInteraction()
    asyncio.run(link_modal("not a link").on_submit(interaction))
    assert interaction.log[0][0] == "send" and interaction.log[0][2]["ephemeral"] is True
    assert len(interaction.log) == 1


def test_貼連結_從Notion讀結果_補回饋並通知(monkeypatch):
    cog = make_cog()
    updates, dms = [], []

    async def get_submission(page_id):
        return SUBMISSION

    async def update(page_id, **kw):
        updates.append((page_id, kw))

    async def notify(submission, **kw):
        dms.append(kw)
        return 1

    async def log_event(**kw):
        pass

    cog._notify_review_feedback_file = notify
    monkeypatch.setattr(repositories, "get_submission", get_submission)
    monkeypatch.setattr(repositories, "update_submission_review", update)
    monkeypatch.setattr(audit, "log_event", log_event)
    interaction = FakeInteraction(cog=cog)
    asyncio.run(link_modal(note="放在老師回饋").on_submit(interaction))
    _, kw = updates[0]
    assert kw["status"] == "需修改" and kw["reviewer"] == "老師｜42"
    assert "副歌再收" in kw["feedback"] and "放在老師回饋" in kw["feedback"]
    assert "批改檔：https://drive.google.com/x" in kw["feedback"]
    assert dms[0]["links_by_artist_page_id"] == {"a1": ["https://drive.google.com/x"]}
    assert interaction.log[-1][2]["view"] is None and "✅" in interaction.log[-1][1]


def test_提交已被清掉_說明並結束(monkeypatch):
    cog = make_cog()

    async def get_submission(page_id):
        return None

    monkeypatch.setattr(repositories, "get_submission", get_submission)
    interaction = FakeInteraction(cog=cog)
    asyncio.run(link_modal().on_submit(interaction))
    assert "已經不在了" in interaction.log[-1][1] and interaction.log[-1][2]["view"] is None


def test_Notion寫入失敗_按鈕留著(monkeypatch):
    cog = make_cog()

    async def get_submission(page_id):
        return SUBMISSION

    async def update(page_id, **kw):
        raise notion_api.NotionAPIError(500, "boom")

    monkeypatch.setattr(repositories, "get_submission", get_submission)
    monkeypatch.setattr(repositories, "update_submission_review", update)
    interaction = FakeInteraction(cog=cog)
    asyncio.run(link_modal().on_submit(interaction))
    assert "❌" in interaction.log[-1][1] and interaction.log[-1][2]["view"].is_persistent()


@pytest.mark.parametrize("page,expected", [
    ({"id": "x", "archived": True, "properties": {}}, False),
    ({"id": "x", "in_trash": True, "properties": {}}, False),
])
def test_垃圾桶裡的提交讀不到(monkeypatch, page, expected):
    async def retrieve(page_id):
        return page

    monkeypatch.setattr(notion_api, "retrieve_page", retrieve)
    assert asyncio.run(repositories.get_submission("x")) is None


def test_提交不存在回None(monkeypatch):
    async def retrieve(page_id):
        raise notion_api.NotionAPIError(404, "not found")

    monkeypatch.setattr(notion_api, "retrieve_page", retrieve)
    assert asyncio.run(repositories.get_submission("x")) is None


def test_交件清單的返回我的任務按得動():
    calls = []

    class Cog:
        async def return_to_my_tasks(self, interaction):
            calls.append(interaction)

    view = submissions.SubmitTaskView(Cog(), [], submissions.datetime.now(submissions.config.TZ))
    asyncio.run(button(view, "返回我的任務").callback("interaction"))
    assert calls == ["interaction"]
