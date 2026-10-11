"""P0-3 提交流程純函式測試。"""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import config
from cogs.submissions import (
    FREE_DISCORD_UPLOAD_MB,
    FILE_UPLOAD_WAIT_MINUTES,
    MAX_UPLOAD_MB,
    PendingFileSubmission,
    PendingReviewFeedbackFile,
    ReviewFeedbackDriveTarget,
    LargeFileSubmissionChoiceView,
    ReviewFeedbackLargeFileChoiceView,
    PostReviewAttachmentView,
    ReviewActionView,
    ReviewSubmissionView,
    SubmitTaskView,
    direct_upload_size_error,
    disable_all_view_items,
    drive_folder_submission_summary,
    expired_pending_keys,
    feedback_with_attachment_links,
    file_upload_wait_label,
    is_valid_link,
    large_file_drive_instruction,
    notify_submission_reviewers,
    notion_page_url,
    prefixed_review_feedback_filename,
    prefixed_submission_filename,
    review_feedback_drive_instruction,
    review_feedback_file_dm_text,
    review_dm_text,
    review_submission_detail_text,
    review_task_status_for_result,
    submit_task_intro_text,
    submit_task_option_description,
    task_links_text,
    parse_revision_days,
)
from cogs import ui_text
from services import notion_links, repositories


def task(
    *,
    status: str | None = "待提交",
    open_for_submission: bool = True,
    # 2026-10-04：原本預設是「課程作業」，那是 Submissions 的檔案類型、
    # 不是 Notion「任務類型」的選項。用真實選項，否則測試會跟程式一起錯而測不出來。
    task_type: str | None = "課程時間&作業繳交",
    description: str = "",
    reviewer_page_ids: list[str] | None = None,
):
    return repositories.TaskItem(
        page_id="task-1",
        name="編曲作業",
        status=status,
        task_type=task_type,
        open_for_submission=open_for_submission,
        description=description,
        reviewer_page_ids=reviewer_page_ids if reviewer_page_ids is not None else [],
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


class TestRevisionDays:
    def test_accepts_one_to_seven_days(self):
        assert parse_revision_days("1") == 1
        assert parse_revision_days("7") == 7

    def test_rejects_empty_non_integer_and_out_of_range(self):
        for value in ("", "0", "8", "兩天", "1.5"):
            assert parse_revision_days(value) is None


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
        assert repositories.submission_file_type_for_task("課程時間&作業繳交") == "課程作業"
        assert repositories.submission_file_type_for_task("創作交付") == "Demo"
        assert repositories.submission_file_type_for_task("企劃提案") == "企劃書"
        assert repositories.submission_file_type_for_task("方晨業配內容") == "業配素材"
        assert repositories.submission_file_type_for_task("錄音") == "錄音檔"

    def test_unknown_type_falls_back_to_other(self):
        assert repositories.submission_file_type_for_task("行政") == "其他"
        assert repositories.submission_file_type_for_task(None) == "其他"

    def test_old_dead_strings_no_longer_map(self):
        """2026-10-04 前程式硬編這些字串，但它們都不是 Notion 的有效選項。

        留這個測試是為了釘住「死值已經清掉」——如果有人把它們加回來，
        守門測試 test_notion_option_values 會先失敗，這裡再補一層。
        """
        assert repositories.submission_file_type_for_task("課程與作業") == "其他"
        assert repositories.submission_file_type_for_task("課程作業") == "其他"
        assert repositories.submission_file_type_for_task("業配內容") == "其他"


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


class TestSubmitTaskFirstStepText:
    def test_intro_lists_tasks_and_teacher_requirements_and_points_to_drive(self):
        now = datetime(2026, 7, 10, 12, 0, tzinfo=config.TZ)
        text = submit_task_intro_text(
            "Johnny",
            [
                task(
                    task_type="課程時間&作業繳交",
                    description="老師要求：請上傳 PDF 或簡報，不要傳壓縮檔。",
                ),
                task(task_type="錄音"),
            ],
            now,
        )

        assert "先選任務" in text
        assert "Drive 資料夾" in text
        assert "老師要求" in text
        assert "PDF" in text
        # 提交方式已停用（M4-03）：不再出現貼連結／填文字
        assert "貼連結" not in text and "填文字" not in text

    def test_intro_stays_within_discord_limit_when_descriptions_are_long(self):
        """任務說明很長時，固定列 10 筆會超過 Discord 上限；應該改成依長度收斂並誠實說明還有幾筆。"""
        now = datetime(2026, 7, 10, 12, 0, tzinfo=config.TZ)
        tasks = [
            task(task_type="課程作業",
                description="老師要求：" + "請詳細說明本次作業的完成方式與注意事項。" * 6,
            )
            for _ in range(12)
        ]

        text = submit_task_intro_text("Johnny", tasks, now)

        assert len(text) <= ui_text.DISCORD_MESSAGE_LIMIT
        assert "...清單過長已截斷" not in text
        assert "請用下方選單查看" in text

    def test_option_description_shows_type_and_deadline(self):
        now = datetime(2026, 7, 10, 12, 0, tzinfo=config.TZ)
        text = submit_task_option_description(task(), now)

        assert "課程時間&作業繳交" in text
        assert "無截止日" in text
        assert "貼連結" not in text
        assert len(text) <= 100


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


class TestArchivedFilename:
    def test_submission_file_gets_artist_and_task_prefix(self):
        filename = prefixed_submission_filename(
            artist_name="Johnny",
            task_name="編曲作業",
            original_filename="week1_demo.mp3",
        )

        assert filename == "Johnny_編曲作業_week1_demo.mp3"

    def test_submission_filename_sanitizes_unsafe_characters_and_keeps_extension(self):
        filename = prefixed_submission_filename(
            artist_name="Johnny/Sun",
            task_name="錄音:第一週?",
            original_filename="my/demo:file?.wav",
        )

        assert filename == "Johnny_Sun_錄音_第一週_my_demo_file.wav"
        assert filename.endswith(".wav")

    def test_review_feedback_file_gets_submission_prefix(self):
        filename = prefixed_review_feedback_filename(
            submission_name="Johnny｜編曲作業",
            original_filename="feedback.pdf",
        )

        assert filename == "老師回饋_Johnny｜編曲作業_feedback.pdf"


class TestLargeFileDriveInstruction:
    def test_with_folder_link_guides_artist_to_upload_and_confirm(self):
        text = large_file_drive_instruction(
            task_name="編曲作業",
            folder_url="https://drive.google.com/drive/folders/abc",
        )

        assert "編曲作業" in text
        assert "資料夾" in text
        assert "https://drive.google.com/drive/folders/abc" in text
        assert "我已上傳到 Drive" in text
        assert "貼 Drive 連結" not in text
        assert "截圖佐證" not in text

    def test_without_folder_link_requires_company_drive_repair(self):
        text = large_file_drive_instruction(task_name="編曲作業", folder_url=None)

        assert "無法自動產生指定資料夾" in text
        assert "聯絡控方" in text
        assert "貼 Drive 連結" not in text

    def test_drive_folder_submission_summary_collects_names_ids_and_size(self):
        names, ids, size_mb = drive_folder_submission_summary([
            {"name": "a.pdf", "id": "file-a", "size": "1048576"},
            {"name": "b.pptx", "id": "file-b", "size": "2097152"},
        ])

        assert names == "a.pdf、b.pptx"
        assert ids == "file-a、file-b"
        assert size_mb == 3.0


class TestReviewFeedbackDriveInstruction:
    def test_with_targets_lists_teacher_feedback_folders(self):
        text = review_feedback_drive_instruction(
            submission_name="Johnny｜編曲作業",
            targets=[
                ReviewFeedbackDriveTarget(
                    artist_page_id="artist-1",
                    artist_name="Johnny",
                    folder_url="https://drive.google.com/drive/folders/abc",
                    base_folder_id="base-1",
                    path_segments=["老師回饋", "2026-07"],
                )
            ],
        )

        assert "Johnny｜編曲作業" in text
        assert "老師回饋資料夾" in text
        assert "Johnny" in text
        assert "https://drive.google.com/drive/folders/abc" in text
        assert "貼 Drive 連結補回饋" in text
        assert "截圖佐證" not in text

    def test_without_targets_falls_back_to_link_feedback(self):
        text = review_feedback_drive_instruction(submission_name="Johnny｜編曲作業", targets=[])

        assert "無法自動產生老師回饋資料夾" in text
        assert "貼 Drive 連結補回饋" in text


class TestPendingFileSubmission:
    def test_expires_after_deadline(self):
        now = datetime(2026, 7, 10, 12, 0, tzinfo=config.TZ)
        assert FILE_UPLOAD_WAIT_MINUTES == 180
        assert file_upload_wait_label() == "3 小時"
        pending = PendingFileSubmission(
            task=task(),
            artist=artist(),
            channel_id=456,
            created_at=now,
            expires_at=now + timedelta(minutes=FILE_UPLOAD_WAIT_MINUTES),
        )

        assert not pending.is_expired(now + timedelta(minutes=179, seconds=59))
        assert pending.is_expired(now + timedelta(minutes=180, seconds=1))


class TestPendingReviewFeedbackFile:
    def test_expires_after_deadline(self):
        now = datetime(2026, 7, 10, 12, 0, tzinfo=config.TZ)
        pending = PendingReviewFeedbackFile(
            submission=repositories.SubmissionItem(page_id="submission-1", name="Johnny｜功課", status="通過"),
            result="通過",
            feedback="很好",
            reviewer="A 老師｜123",
            channel_id=456,
            created_at=now,
            expires_at=now + timedelta(minutes=FILE_UPLOAD_WAIT_MINUTES),
        )

        assert not pending.is_expired(now + timedelta(minutes=179, seconds=59))
        assert pending.is_expired(now + timedelta(minutes=180, seconds=1))


class TestExpiredPendingKeys:
    """穩定度加固：使用者中途放棄上傳時，pending dict 要能被背景清理掃到。"""

    def test_returns_only_expired_keys(self):
        now = datetime(2026, 7, 10, 12, 0, tzinfo=config.TZ)
        fresh = PendingFileSubmission(
            task=task(), artist=artist(), channel_id=1,
            created_at=now, expires_at=now + timedelta(minutes=10),
        )
        stale = PendingFileSubmission(
            task=task(), artist=artist(), channel_id=1,
            created_at=now - timedelta(hours=4), expires_at=now - timedelta(minutes=1),
        )
        pending_by_user = {111: fresh, 222: stale}

        assert expired_pending_keys(pending_by_user, now) == [222]

    def test_empty_dict_returns_empty_list(self):
        assert expired_pending_keys({}, datetime(2026, 7, 10, 12, 0, tzinfo=config.TZ)) == []


class TestReviewDisplayHelpers:
    def test_notion_page_url_removes_dashes(self):
        assert notion_page_url("abcd-1234") == "https://www.notion.so/abcd1234"
        assert notion_page_url("") == ""

    def test_task_links_text_builds_related_task_links(self):
        text = task_links_text(["task-1", "task-2"])

        assert "[任務 1]" in text
        assert "task1" in text
        assert "[任務 2]" in text

    def test_review_submission_detail_includes_drive_note_submission_and_tasks(self):
        submission = repositories.SubmissionItem(
            page_id="submission-1",
            name="Johnny｜編曲作業",
            status="待審核",
            submit_type="檔案",
            drive_url="https://drive.google.com/file/d/abc",
            note="這是一段提交說明",
            url="https://notion.so/submission",
            task_page_ids=["task-1"],
        )

        text = review_submission_detail_text(submission)

        assert "Drive" in text
        assert "這是一段提交說明" in text
        assert "Notion 提交頁" in text
        assert "關聯任務" in text
        assert "附批改檔" in text

    def test_feedback_with_attachment_links_appends_drive_links(self):
        text = feedback_with_attachment_links("副歌再修", ["https://drive/a", "https://drive/b"])

        assert "副歌再修" in text
        assert "批改檔：https://drive/a、https://drive/b" in text

    def test_review_result_dm_gives_no_notion_entry_point_to_the_artist(self):
        """任務頁往上翻就是整個 Tasks，藝人端的審核 DM 一律不附 Notion 連結。"""
        submission = repositories.SubmissionItem(
            page_id="submission-1",
            name="Johnny｜編曲作業",
            status="需修改",
            task_page_ids=["3b55806f-097b-81e8-b430-e9b9ce902f2a"],
        )

        text = review_dm_text(
            submission,
            task_name="測試課程",
            result="需修改",
            feedback="請調整音量",
            reviewer="A 老師",
        )

        assert not notion_links.contains_notion_link(text)
        # 藝人仍要知道審的是哪一筆、老師說了什麼，以及下一步怎麼做。
        assert "測試課程" in text
        assert "請調整音量" in text
        assert "/我的任務" in text

    def test_finishing_review_disables_every_action_in_the_view(self):
        class Item:
            disabled = False

        class View:
            children = [Item(), Item()]

        view = View()
        disable_all_view_items(view)

        assert all(item.disabled for item in view.children)

    def test_no_feedback_file_removes_completed_review_actions(self):
        class Response:
            edited_message = None

            async def edit_message(self, **kwargs):
                self.edited_message = kwargs

            async def send_message(self, *args, **kwargs):
                raise AssertionError("reviewer should be allowed to finish the review")

        response = Response()
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=123),
            response=response,
        )
        submission = repositories.SubmissionItem(
            page_id="submission-1",
            name="Johnny｜測試任務",
            status="待審核",
        )
        view = PostReviewAttachmentView(
            SimpleNamespace(),
            submission,
            result="通過",
            feedback="很好",
            reviewer="A 老師",
            reviewer_id=123,
        )
        no_feedback_button = next(
            item for item in view.children if getattr(item, "label", "") == "不需補批改檔"
        )

        asyncio.run(no_feedback_button.callback(interaction))

        assert response.edited_message == {
            "content": "✅ 已確認不需補批改檔。審核流程完成。",
            "view": None,
        }

    def test_submission_and_review_views_expose_navigation(self):
        now = datetime(2026, 8, 12, tzinfo=config.TZ)
        submit_task = repositories.TaskItem(
            page_id="task-1", name="測試任務", status="待提交", task_type="作業"
        )
        submission = repositories.SubmissionItem(
            page_id="submission-1", name="Johnny｜測試任務", status="待審核"
        )
        cog = SimpleNamespace()

        task_view = SubmitTaskView(cog, [submit_task], now)
        assert any(getattr(item, "label", "") == "返回我的任務" for item in task_view.children)

        review_list = ReviewSubmissionView(cog, [submission])
        assert any(getattr(item, "label", "") == "取消" for item in review_list.children)

        review_action = ReviewActionView(cog, submission, 123, [submission])
        assert any(getattr(item, "label", "") == "返回審核清單" for item in review_action.children)

        drive_view = LargeFileSubmissionChoiceView(cog, submit_task, owner_id=123)
        assert any(getattr(item, "label", "") == "返回任務選擇" for item in drive_view.children)

        feedback_view = ReviewFeedbackLargeFileChoiceView(
            cog,
            submission,
            result="通過",
            feedback="很好",
            reviewer="老師",
            targets=[],
            owner_id=123,
        )
        assert any(getattr(item, "label", "") == "返回回饋選擇" for item in feedback_view.children)

    def test_review_feedback_file_dm_mentions_result_feedback_and_file(self):
        submission = repositories.SubmissionItem(
            page_id="submission-1",
            name="Johnny｜編曲作業",
            status="通過",
            task_page_ids=["task-1"],
        )

        text = review_feedback_file_dm_text(
            submission,
            result="通過",
            feedback="做得很好",
            reviewer="A 老師",
            file_links=["https://drive/review"],
        )

        assert "批改檔" in text
        assert "通過" in text
        assert "做得很好" in text
        # Drive 批改檔連結是藝人該拿到的；只有 Notion 入口不給。
        assert "https://drive/review" in text
        assert not notion_links.contains_notion_link(text)


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


class _FakeDMChannel:
    """假的控方頻道：只記下送出的訊息，不真的打 Discord。"""

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)


class _FakeDiscordUser:
    """假的審核人員 Discord 帳號：只記下收到的私訊。"""

    def __init__(self, user_id: int):
        self.user_id = user_id
        self.sent: list[str] = []

    async def send(self, content):
        self.sent.append(content)


class _FakeClient:
    """假的 discord.Client：get_channel 同步、fetch_user 非同步，跟真的介面一致即可。"""

    def __init__(self, channel=None, users: dict[int, "_FakeDiscordUser"] | None = None):
        self._channel = channel
        self._users = users or {}

    def get_channel(self, _channel_id):
        return self._channel

    async def fetch_user(self, user_id):
        user = self._users.get(int(user_id))
        if user is None:
            raise ValueError(f"no such discord user: {user_id}")
        return user


class TestNotifySubmissionReviewers:
    """M4-15：審核人員 有填就私訊那些人，沒填才退回控方頻道——不是兩邊都發。"""

    def _patch_people(self, monkeypatch, people_by_page_id: dict[str, repositories.PersonItem]):
        async def fake_get_people_by_ids(page_ids):
            return [people_by_page_id[pid] for pid in page_ids if pid in people_by_page_id]
        monkeypatch.setattr(repositories, "get_people_by_ids", fake_get_people_by_ids)

    def test_no_reviewer_set_falls_back_to_admin_channel(self, monkeypatch):
        self._patch_people(monkeypatch, {})
        channel = _FakeDMChannel()
        client = _FakeClient(channel=channel)

        asyncio.run(notify_submission_reviewers(client, task(reviewer_page_ids=[]), "msg"))

        assert channel.sent == [{"content": "msg"}]

    def test_reviewer_without_discord_id_falls_back_to_admin_channel(self, monkeypatch):
        """審核人員欄位有填人，但那個人缺 Discord ID——視同沒有可通知對象，
        不能默默不通知任何人（QA 卡的反向對照）。"""
        self._patch_people(monkeypatch, {
            "p1": repositories.PersonItem(page_id="p1", name="甲", discord_user_id=""),
        })
        channel = _FakeDMChannel()
        client = _FakeClient(channel=channel)

        asyncio.run(notify_submission_reviewers(client, task(reviewer_page_ids=["p1"]), "msg"))

        assert channel.sent == [{"content": "msg"}]

    def test_single_reviewer_is_dmed_and_admin_channel_stays_silent(self, monkeypatch):
        """有指定審核人員時是純 fallback，不是「兩邊都發」——
        跟 cogs/reminders.py 待審核催辦（兩邊都發）不同，別混淆。"""
        self._patch_people(monkeypatch, {
            "p1": repositories.PersonItem(page_id="p1", name="甲", discord_user_id="111"),
        })
        channel = _FakeDMChannel()
        jia = _FakeDiscordUser(111)
        client = _FakeClient(channel=channel, users={111: jia})

        asyncio.run(notify_submission_reviewers(client, task(reviewer_page_ids=["p1"]), "msg"))

        assert jia.sent == ["msg"]
        assert channel.sent == []

    def test_switching_reviewer_only_notifies_the_new_one(self, monkeypatch):
        """改了審核人員，收通知的人要跟著改；舊的審核人員不該再收到。"""
        self._patch_people(monkeypatch, {
            "p1": repositories.PersonItem(page_id="p1", name="甲", discord_user_id="111"),
            "p2": repositories.PersonItem(page_id="p2", name="乙", discord_user_id="222"),
        })
        jia, yi = _FakeDiscordUser(111), _FakeDiscordUser(222)
        client = _FakeClient(channel=_FakeDMChannel(), users={111: jia, 222: yi})

        asyncio.run(notify_submission_reviewers(client, task(reviewer_page_ids=["p2"]), "msg"))

        assert yi.sent == ["msg"]
        assert jia.sent == []

    def test_multiple_reviewers_all_notified(self, monkeypatch):
        self._patch_people(monkeypatch, {
            "p1": repositories.PersonItem(page_id="p1", name="甲", discord_user_id="111"),
            "p2": repositories.PersonItem(page_id="p2", name="乙", discord_user_id="222"),
        })
        jia, yi = _FakeDiscordUser(111), _FakeDiscordUser(222)
        client = _FakeClient(channel=_FakeDMChannel(), users={111: jia, 222: yi})

        asyncio.run(notify_submission_reviewers(
            client, task(reviewer_page_ids=["p1", "p2"]), "msg",
        ))

        assert jia.sent == ["msg"]
        assert yi.sent == ["msg"]

    def test_unreachable_reviewer_does_not_raise_or_fall_back(self, monkeypatch):
        """私訊失敗（例如 bot 跟那個人沒有共同伺服器）只記 log，不炸流程，
        也不會因此悄悄改發控方頻道——那樣控方會以為沒人被通知，但其實已經試過了。"""
        self._patch_people(monkeypatch, {
            "p1": repositories.PersonItem(page_id="p1", name="甲", discord_user_id="999"),
        })
        channel = _FakeDMChannel()
        client = _FakeClient(channel=channel, users={})  # 999 抓不到

        asyncio.run(notify_submission_reviewers(client, task(reviewer_page_ids=["p1"]), "msg"))

        assert channel.sent == []


class TestSubmitAlwaysUsesDrive:
    """M4-03：提交方式已停用，選任務後一律進入公司 Drive 上傳，不再開連結／文字表單。"""

    def test_selecting_a_task_always_starts_drive_upload(self):
        from cogs.submissions import SubmitTaskSelect

        calls = []

        class Cog:
            async def start_file_upload_wait(self, interaction, task, drive_only=False):
                calls.append((task.page_id, drive_only))

        now = datetime(2026, 10, 11, tzinfo=config.TZ)
        submit_task = task()
        select = SubmitTaskSelect(Cog(), [submit_task], now)
        select._values = [submit_task.page_id]
        interaction = SimpleNamespace(response=SimpleNamespace(send_modal=None, send_message=None))

        asyncio.run(select.callback(interaction))

        assert calls == [("task-1", True)]
