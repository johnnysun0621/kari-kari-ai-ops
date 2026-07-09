"""P0-3 提交流程純函式測試。"""

from datetime import datetime, timedelta

import config
from cogs.submissions import (
    FREE_DISCORD_UPLOAD_MB,
    FILE_UPLOAD_WAIT_MINUTES,
    MAX_UPLOAD_MB,
    PendingFileSubmission,
    direct_upload_size_error,
    file_upload_wait_label,
    is_valid_link,
    modal_field_spec,
    review_task_status_for_result,
    submission_method_conflict,
    submission_type_for_input,
    submit_hint_for_task,
    submit_method_label,
)
from services import repositories


def task(
    *,
    status: str | None = "待提交",
    open_for_submission: bool = True,
    task_type: str | None = "課程作業",
    submit_method: str | None = None,
):
    return repositories.TaskItem(
        page_id="task-1",
        name="編曲作業",
        status=status,
        task_type=task_type,
        open_for_submission=open_for_submission,
        submit_method=submit_method,
    )


def artist():
    return repositories.Artist(page_id="artist-1", name="Johnny", discord_user_id="123")


class TestIsTaskSubmittable:
    def test_open_active_task_is_submittable(self):
        assert repositories.is_task_submittable(task(status="待提交"))
        assert repositories.is_task_submittable(task(status="已逾期"))
        assert repositories.is_task_submittable(task(status=None))

    def test_closed_flag_blocks_submission(self):
        assert not repositories.is_task_submittable(task(open_for_submission=False))

    def test_reviewing_or_finished_tasks_are_not_submittable(self):
        for status in ("待審核", "已完成", "已取消", "已封存"):
            assert not repositories.is_task_submittable(task(status=status)), status


class TestSubmissionTypeForInput:
    def test_link_only(self):
        assert submission_type_for_input("https://a", "") == "Drive 連結"

    def test_text_only(self):
        assert submission_type_for_input("", "內容") == "文字"

    def test_both_is_mixed(self):
        assert submission_type_for_input("https://a", "內容") == "混合"

    def test_neither_is_rejected(self):
        assert submission_type_for_input("", "") is None


class TestLinkValidation:
    def test_accepts_http_and_https(self):
        assert is_valid_link("https://drive.google.com/x")
        assert is_valid_link("http://example.com")

    def test_rejects_non_links(self):
        assert not is_valid_link("drive.google.com/x")
        assert not is_valid_link("https://a b")
        assert not is_valid_link("")


class TestSubmissionFileType:
    def test_maps_known_task_types(self):
        assert repositories.submission_file_type_for_task("課程作業") == "課程作業"
        assert repositories.submission_file_type_for_task("創作交付") == "Demo"
        assert repositories.submission_file_type_for_task("企劃提案") == "企劃書"
        assert repositories.submission_file_type_for_task("錄音") == "錄音檔"

    def test_unknown_type_falls_back_to_other(self):
        assert repositories.submission_file_type_for_task("行政") == "其他"
        assert repositories.submission_file_type_for_task(None) == "其他"


class TestBuildSubmissionProps:
    def test_link_submission_props(self):
        props = repositories._build_submission_props(
            task(), artist(),
            submit_type="Drive 連結",
            link_url="https://drive.google.com/x",
            now=datetime(2026, 7, 10, 14, 30, tzinfo=config.TZ),
        )

        assert props["提交來源"]["select"]["name"] == "Discord"
        assert props["提交類型"]["select"]["name"] == "Drive 連結"
        assert props["審核狀態"]["select"]["name"] == "待審核"
        assert props["權限等級"]["select"]["name"] == "藝人本人"
        assert props["是否最新版"]["checkbox"] is True
        assert props["Drive URL"]["url"] == "https://drive.google.com/x"
        assert props["檔案類型"]["select"]["name"] == "課程作業"
        assert props["關聯任務"]["relation"] == [{"id": "task-1"}]
        assert props["提交藝人"]["relation"] == [{"id": "artist-1"}]
        assert "備註" not in props

    def test_text_submission_puts_content_in_note(self):
        props = repositories._build_submission_props(
            task(), artist(), submit_type="文字", content="本週練習心得",
        )

        assert props["備註"]["rich_text"][0]["text"]["content"] == "本週練習心得"
        assert "Drive URL" not in props


class TestReviewUpdateProps:
    def test_builds_review_props(self):
        reviewed_at = datetime(2026, 7, 10, 15, 0, tzinfo=config.TZ)
        props = repositories.review_update_props(
            status="需修改",
            feedback="副歌音準再修一下",
            reviewer="A 老師｜123",
            source="Discord",
            notified_at=reviewed_at,
        )

        assert props["審核狀態"]["select"]["name"] == "需修改"
        assert props["審核回饋"]["rich_text"][0]["text"]["content"] == "副歌音準再修一下"
        assert props["審核者"]["rich_text"][0]["text"]["content"] == "A 老師｜123"
        assert props["審核來源"]["select"]["name"] == "Discord"
        assert props["審核通知時間"]["date"]["start"] == reviewed_at.isoformat()


class TestSubmitHint:
    def test_file_method_points_to_same_channel_upload(self):
        hint = submit_hint_for_task(task(submit_method="檔案"))
        assert "同一個頻道" in hint
        assert "私訊" not in hint
        assert "尚未開通" not in hint  # 舊文案不得復活


class TestSubmissionMethodConflict:
    def test_file_task_without_attachment_can_start_dm_upload(self):
        msg = submission_method_conflict(task(submit_method="檔案"), has_attachment=False)
        assert msg is None

    def test_link_or_text_task_rejects_attachment(self):
        assert submission_method_conflict(task(submit_method="Drive 連結"), True) is not None
        assert submission_method_conflict(task(submit_method="文字"), True) is not None

    def test_matching_combinations_pass(self):
        assert submission_method_conflict(task(submit_method="檔案"), True) is None
        assert submission_method_conflict(task(submit_method="Drive 連結"), False) is None
        assert submission_method_conflict(task(submit_method="文字"), False) is None
        assert submission_method_conflict(task(submit_method="混合"), True) is None
        assert submission_method_conflict(task(submit_method=None), False) is None


class TestModalFieldSpec:
    def test_link_task_requires_link(self):
        assert modal_field_spec("Drive 連結") == (True, True, True, False)

    def test_text_task_only_text_required(self):
        assert modal_field_spec("文字") == (False, False, True, True)

    def test_mixed_or_unset_shows_both_optional(self):
        assert modal_field_spec("混合") == (True, False, True, False)
        assert modal_field_spec(None) == (True, False, True, False)


class TestSubmitMethodLabel:
    def test_labels_each_method(self):
        assert "📎" in submit_method_label("檔案")
        assert "🔗" in submit_method_label("Drive 連結")
        assert "📝" in submit_method_label("文字")
        assert submit_method_label(None)  # 未指定也有預設標示

    def test_text_method_asks_for_text(self):
        assert "文字" in submit_hint_for_task(task(submit_method="文字"))


class TestUploadBoundary:
    def test_free_discord_upload_standard_is_10mb(self):
        assert FREE_DISCORD_UPLOAD_MB == 10
        assert MAX_UPLOAD_MB == 10

    def test_rejects_files_above_free_discord_standard(self):
        message = direct_upload_size_error((FREE_DISCORD_UPLOAD_MB * 1024 * 1024) + 1)

        assert message is not None
        assert "10MB" in message
        assert "Drive" in message

    def test_accepts_files_at_free_discord_standard(self):
        assert direct_upload_size_error(FREE_DISCORD_UPLOAD_MB * 1024 * 1024) is None


class TestPendingFileSubmission:
    def test_expires_after_deadline(self):
        now = datetime(2026, 7, 10, 12, 0, tzinfo=config.TZ)
        assert FILE_UPLOAD_WAIT_MINUTES == 180
        assert file_upload_wait_label() == "3 小時"
        pending = PendingFileSubmission(
            task=task(submit_method="檔案"),
            artist=artist(),
            channel_id=456,
            created_at=now,
            expires_at=now + timedelta(minutes=FILE_UPLOAD_WAIT_MINUTES),
        )

        assert not pending.is_expired(now + timedelta(minutes=179, seconds=59))
        assert pending.is_expired(now + timedelta(minutes=180, seconds=1))


class TestReviewResultMapping:
    def test_review_result_updates_task_status(self):
        assert review_task_status_for_result("通過") == "已完成"
        assert review_task_status_for_result("需修改") == "需修改"
        assert review_task_status_for_result("退回") == "需修改"
        assert review_task_status_for_result("待審核") is None

    def test_notion_auto_notify_requires_review_context(self):
        submission = repositories.SubmissionItem(
            page_id="submission-1",
            name="Johnny｜編曲作業",
            status="通過",
        )

        assert not repositories.review_submission_ready_for_auto_notify(submission)

        with_feedback = repositories.SubmissionItem(
            page_id="submission-1",
            name="Johnny｜編曲作業",
            status="通過",
            review_feedback="做得很好",
        )
        with_source = repositories.SubmissionItem(
            page_id="submission-1",
            name="Johnny｜編曲作業",
            status="通過",
            review_source="Notion",
        )

        assert repositories.review_submission_ready_for_auto_notify(with_feedback)
        assert repositories.review_submission_ready_for_auto_notify(with_source)
