"""作業歸檔路徑邏輯測試（藝人專區→專案夾→任務夾，2026-10-13 改版，規則 M4-07）。"""

from datetime import datetime

import config
from services import drive_archive, repositories

NOW = datetime(2026, 7, 10, 15, 0, tzinfo=config.TZ)


def task(task_type: str | None = "課程時間&作業繳交", drive_folder_url: str = ""):
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


PROJECT = "方晨＿哈根達斯雪酥限定"


class TestProjectFolder:
    def test_uses_notion_project_name_as_is(self):
        # 全形底線、全形括號、半形空格都照 Notion 原字串，控方看到的名字就是 Drive 上的名字
        assert drive_archive.project_folder_name(PROJECT) == PROJECT
        assert drive_archive.project_folder_name("方晨2610 (發文）") == "方晨2610 (發文）"

    def test_no_project_goes_to_unassigned(self):
        assert drive_archive.project_folder_name(None) == "未分專案"
        assert drive_archive.project_folder_name("   ") == "未分專案"

    def test_drive_unsafe_chars_are_replaced(self):
        assert drive_archive.project_folder_name("A/B:C") == "A_B_C"

    def test_task_type_no_longer_affects_folder(self):
        """舊結構依任務類型分夾，任務類型大多沒填 → 幾乎全進「其他」。改版後只看專案。"""
        a = drive_archive.archive_path_for_submission(task("課程時間&作業繳交"), NOW, PROJECT)
        b = drive_archive.archive_path_for_submission(task(None), NOW, PROJECT)
        assert a == b


class TestArchivePath:
    def test_path_is_project_then_task(self):
        assert drive_archive.archive_path_for_submission(task(), NOW, PROJECT) == [
            PROJECT, "2026.07.10_編曲作業",
        ]

    def test_no_type_folder_or_month_layer(self):
        path = drive_archive.archive_path_for_submission(task(), NOW, PROJECT)
        assert "其他" not in path and "2026-07" not in path

    def test_task_without_project(self):
        assert drive_archive.archive_path_for_submission(task(), NOW) == [
            "未分專案", "2026.07.10_編曲作業",
        ]

    def test_review_feedback_path_is_under_task_folder(self):
        assert drive_archive.archive_path_for_review_feedback(task(), NOW, PROJECT) == [
            PROJECT, "2026.07.10_編曲作業", "老師回饋",
        ]

    def test_task_name_is_sanitized_and_task_id_makes_folder_stable(self):
        current = task()
        current.name = '表演/藝術：第一版'
        assert drive_archive.task_folder_name(current, NOW) == "2026.07.10_表演_藝術：第一版"

    def test_task_folder_uses_deadline_when_available(self):
        current = task()
        current.end = datetime(2026, 7, 25, tzinfo=config.TZ)
        assert drive_archive.task_folder_name(current, NOW) == "2026.07.25_編曲作業"

    def test_task_folder_uses_notion_created_at_without_deadline(self):
        current = task()
        current.created_at = datetime(2026, 7, 9, 23, 30, tzinfo=config.TZ)
        assert drive_archive.task_folder_name(current, NOW) == "2026.07.09_編曲作業"

    def test_review_feedback_folder_prefix_is_versioned(self):
        assert drive_archive.versioned_folder_prefix("老師回饋", NOW) == "老師回饋_2026.07.10_ver."

    def test_submission_folder_prefix_contains_task_date_and_version_marker(self):
        assert drive_archive.versioned_folder_prefix("編曲/作業", NOW) == "編曲_作業_2026.07.10_ver."

    def test_next_version_ignores_other_dates_and_malformed_names(self):
        prefix = "編曲作業_2026.07.10_ver."

        assert drive_archive.next_versioned_folder_name(
            [
                "編曲作業_2026.07.10_ver.1",
                "編曲作業_2026.07.10_ver.3",
                "編曲作業_2026.07.09_ver.8",
                "編曲作業_2026.07.10_ver.x",
            ],
            prefix,
        ) == "編曲作業_2026.07.10_ver.4"

    def test_first_version_starts_at_one(self):
        assert drive_archive.next_versioned_folder_name([], "編曲作業_2026.07.10_ver.") == (
            "編曲作業_2026.07.10_ver.1"
        )


class TestResolveArchiveTarget:
    ZONE = "https://drive.google.com/drive/folders/ZoneId123"

    def test_task_folder_overrides_zone(self):
        t = task(drive_folder_url="https://drive.google.com/drive/folders/TaskFolder9")

        assert drive_archive.resolve_archive_target(t, self.ZONE, NOW) == ("TaskFolder9", [])

    def test_zone_with_project_path(self):
        assert drive_archive.resolve_archive_target(task(), self.ZONE, NOW, PROJECT) == (
            "ZoneId123", [PROJECT, "2026.07.10_編曲作業"],
        )

    def test_task_folder_wins_even_if_project_renamed(self):
        """第一次提交後回寫的任務夾優先：專案事後改名，同一任務仍回到原本的夾。"""
        t = task(drive_folder_url="https://drive.google.com/drive/folders/TaskFolder9")
        assert drive_archive.resolve_archive_target(t, self.ZONE, NOW, "改過的名字") == ("TaskFolder9", [])

    def test_no_usable_folder_returns_none(self):
        assert drive_archive.resolve_archive_target(task(), "", NOW) is None
        assert drive_archive.resolve_archive_target(task(), "https://example.com", NOW) is None


class TestResolveReviewFeedbackTarget:
    def test_zone_with_review_feedback_path(self):
        assert drive_archive.resolve_review_feedback_target("https://drive.google.com/drive/folders/ZoneId123", NOW) == (
            "ZoneId123", ["老師回饋", "2026-07"],
        )

    def test_task_project_path(self):
        assert drive_archive.resolve_review_feedback_target(
            "https://drive.google.com/drive/folders/ZoneId123", NOW, task(), PROJECT
        ) == ("ZoneId123", [PROJECT, "2026.07.10_編曲作業", "老師回饋"])

    def test_written_back_task_folder_holds_feedback(self):
        t = task(drive_folder_url="https://drive.google.com/drive/folders/TaskFolder9")
        assert drive_archive.resolve_review_feedback_target(
            "https://drive.google.com/drive/folders/ZoneId123", NOW, t, "改過的名字"
        ) == ("TaskFolder9", ["老師回饋"])

    def test_no_usable_folder_returns_none(self):
        assert drive_archive.resolve_review_feedback_target("", NOW) is None
        assert drive_archive.resolve_review_feedback_target("https://example.com", NOW) is None
