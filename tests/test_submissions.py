"""P0-3 提交流程純函式測試。"""

from datetime import datetime

import config
from cogs.submissions import is_valid_link, submission_type_for_input, submit_hint_for_task
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


class TestSubmitHint:
    def test_file_method_redirects_to_link(self):
        assert "檔案上傳尚未開通" in submit_hint_for_task(task(submit_method="檔案"))

    def test_text_method_asks_for_text(self):
        assert "文字" in submit_hint_for_task(task(submit_method="文字"))
