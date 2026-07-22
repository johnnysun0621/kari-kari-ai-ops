"""作業歸檔路徑邏輯測試（藝人專區→類型夾→月份夾，2026-07-10 拍板）。"""

from datetime import datetime

import config
from services import drive_archive, repositories

NOW = datetime(2026, 7, 10, 15, 0, tzinfo=config.TZ)


def task(task_type: str | None = "課程作業", drive_folder_url: str = ""):
    return repositories.TaskItem(
        page_id="task-1",
        name="編曲作業",
        status="待提交",
        task_type=task_type,
        drive_folder_url=drive_folder_url,
        task_id=12,
    )


class TestExtractDriveFolderId:
    def test_standard_folder_url(self):
        url = "https://drive.google.com/drive/folders/1AbC_dEf-123?usp=sharing"
        assert drive_archive.extract_drive_folder_id(url) == "1AbC_dEf-123"

    def test_user_scoped_folder_url(self):
        url = "https://drive.google.com/drive/u/0/folders/1XyZ987"
        assert drive_archive.extract_drive_folder_id(url) == "1XyZ987"

    def test_open_id_url(self):
        url = "https://drive.google.com/open?id=1QwE456"
        assert drive_archive.extract_drive_folder_id(url) == "1QwE456"

    def test_non_folder_url_returns_none(self):
        assert drive_archive.extract_drive_folder_id("https://example.com/x") is None
        assert drive_archive.extract_drive_folder_id("") is None


class TestTypeFolderMapping:
    def test_each_type_gets_own_folder(self):
        assert drive_archive.type_folder_for_task("課程與作業") == "課程與作業"
        assert drive_archive.type_folder_for_task("課程作業") == "課程作業"
        assert drive_archive.type_folder_for_task("創作交付") == "創作交付"
        assert drive_archive.type_folder_for_task("業配內容") == "業配內容"

    def test_unknown_type_falls_back(self):
        assert drive_archive.type_folder_for_task(None) == "其他"
        assert drive_archive.type_folder_for_task("神秘類型") == "其他"

    def test_tenant_can_override_mapping(self):
        mapping = {"課程作業": "功課"}
        assert drive_archive.type_folder_for_task("課程作業", mapping) == "功課"
        assert drive_archive.type_folder_for_task("創作交付", mapping) == "其他"


class TestArchivePath:
    def test_path_is_type_folder_then_month(self):
        assert drive_archive.archive_path_for_submission(task(), NOW) == [
            "課程作業", "2026-07", "編曲作業｜建立2026-07-10",
        ]

    def test_review_feedback_path_is_fixed_folder_then_month(self):
        assert drive_archive.archive_path_for_review_feedback(task(), NOW) == [
            "課程作業", "2026-07", "編曲作業｜建立2026-07-10", "老師回饋",
        ]

    def test_task_name_is_sanitized_and_task_id_makes_folder_stable(self):
        current = task()
        current.name = '表演/藝術：第一版'
        assert drive_archive.task_folder_name(current, NOW) == "表演_藝術：第一版｜建立2026-07-10"

    def test_task_folder_uses_deadline_when_available(self):
        current = task()
        current.end = datetime(2026, 7, 25, tzinfo=config.TZ)
        assert drive_archive.task_folder_name(current, NOW) == "編曲作業｜截止2026-07-25"

    def test_task_folder_uses_notion_created_at_without_deadline(self):
        current = task()
        current.created_at = datetime(2026, 7, 9, 23, 30, tzinfo=config.TZ)
        assert drive_archive.task_folder_name(current, NOW) == "編曲作業｜建立2026-07-09"

    def test_review_feedback_folder_name_is_timestamped(self):
        assert drive_archive.review_feedback_folder_name(NOW) == "回饋_20260710_1500"


class TestResolveArchiveTarget:
    ZONE = "https://drive.google.com/drive/folders/ZoneId123"

    def test_task_folder_overrides_zone(self):
        t = task(drive_folder_url="https://drive.google.com/drive/folders/TaskFolder9")

        assert drive_archive.resolve_archive_target(t, self.ZONE, NOW) == ("TaskFolder9", [])

    def test_zone_with_type_and_month_path(self):
        assert drive_archive.resolve_archive_target(task(), self.ZONE, NOW) == (
            "ZoneId123", ["課程作業", "2026-07", "編曲作業｜建立2026-07-10"],
        )

    def test_no_usable_folder_returns_none(self):
        assert drive_archive.resolve_archive_target(task(), "", NOW) is None
        assert drive_archive.resolve_archive_target(task(), "https://example.com", NOW) is None


class TestResolveReviewFeedbackTarget:
    def test_zone_with_review_feedback_path(self):
        assert drive_archive.resolve_review_feedback_target("https://drive.google.com/drive/folders/ZoneId123", NOW) == (
            "ZoneId123", ["老師回饋", "2026-07"],
        )

    def test_no_usable_folder_returns_none(self):
        assert drive_archive.resolve_review_feedback_target("", NOW) is None
        assert drive_archive.resolve_review_feedback_target("https://example.com", NOW) is None
