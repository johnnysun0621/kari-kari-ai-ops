"""repositories.py pure helper tests."""

import asyncio
import dataclasses
from datetime import datetime

import pytest

import config
from services import repositories


async def _no_projects(_artist_page_id: str):
    """撞課情境測試只關心 Tasks；Projects 沒有「Discord 顯示」的概念。"""
    return []


def artist(name: str, discord_user_id: str = "123", status: str | None = "已簽約"):
    return repositories.Artist(
        page_id=f"page-{name}",
        name=name,
        discord_user_id=discord_user_id,
        status=status,
    )


class TestSchedulableArtists:
    def test_keeps_artist_with_discord_id_and_signed_status(self):
        artists = [artist("A", discord_user_id="123", status="已簽約")]

        assert repositories.filter_schedulable_artists(artists) == artists

    def test_drops_artist_without_discord_id(self):
        artists = [artist("A", discord_user_id="", status="已簽約")]

        assert repositories.filter_schedulable_artists(artists) == []

    def test_drops_paused_and_archived_artists(self):
        artists = [
            artist("Unsigned", discord_user_id="789", status="未簽"),
            artist("Paused", discord_user_id="123", status="暫停"),
            artist("Archived", discord_user_id="456", status="封存"),
        ]

        assert repositories.filter_schedulable_artists(artists) == []

    def test_keeps_artist_with_discord_id_and_empty_status(self):
        artists = [artist("A", discord_user_id="123", status=None)]

        assert repositories.filter_schedulable_artists(artists) == artists


class TestAvailabilitySlotProps:
    def test_slot_uses_finalized_monthly_pool_schema(self):
        props = repositories._build_availability_slot_props(
            artist=artist("Johnny"),
            start=datetime(2026, 8, 5, 14, 0, tzinfo=config.TZ),
            end=datetime(2026, 8, 5, 18, 0, tzinfo=config.TZ),
            is_late=False,
            note="收集月份：2026-08",
        )

        assert "關聯排課需求" not in props
        assert props["填寫人名稱"]["rich_text"][0]["text"]["content"] == "Johnny"
        assert props["身份"]["select"]["name"] == "藝人"
        assert props["來源"]["select"]["name"] == "Discord"
        assert props["備註"]["rich_text"][0]["text"]["content"] == "收集月份：2026-08"


class TestTaskDriveFolder:
    def test_appends_labeled_link_for_multi_artist_task(self):
        description = repositories.append_drive_folder_link_to_description(
            "任務內容", "Johnny", "https://drive.google.com/drive/folders/johnny"
        )

        assert description.endswith(
            "Drive 資料夾索引：\n・Johnny：https://drive.google.com/drive/folders/johnny"
        )

    def test_drive_folder_link_append_is_idempotent(self):
        description = "任務內容\n\nDrive 資料夾索引：\n・Johnny：https://drive.google.com/drive/folders/johnny"

        assert repositories.append_drive_folder_link_to_description(
            description, "Johnny", "https://drive.google.com/drive/folders/johnny"
        ) == description

    def test_writes_task_drive_folder_url(self, monkeypatch):
        calls = []

        async def fake_update_page(page_id, properties):
            calls.append((page_id, properties))

        monkeypatch.setattr(repositories.notion_api, "update_page", fake_update_page)

        asyncio.run(repositories.set_task_drive_folder("task-1", "https://drive.google.com/drive/folders/task"))

        assert calls == [
            ("task-1", {"Google Drive Folder": {"url": "https://drive.google.com/drive/folders/task"}})
        ]

    def test_does_not_write_empty_folder_url(self, monkeypatch):
        async def fail_update_page(*args, **kwargs):
            raise AssertionError("empty folder URL should not be written")

        monkeypatch.setattr(repositories.notion_api, "update_page", fail_update_page)
        asyncio.run(repositories.set_task_drive_folder("task-1", ""))


class TestTaskConcernReset:
    def test_returns_task_to_unpublished_state(self, monkeypatch):
        calls = []

        async def fake_update_page(page_id, properties):
            calls.append((page_id, properties))

        monkeypatch.setattr(repositories.notion_api, "update_page", fake_update_page)

        asyncio.run(repositories.return_task_after_artist_concern("task-1"))

        assert calls == [
            (
                "task-1",
                {
                    "任務狀態": {"select": {"name": "尚未開始"}},
                    "Discord 顯示": {"checkbox": False},
                    "Discord 通知時間": {"date": None},
                    "Discord 收到時間": {"date": None},
                    # 退回再派發，舊的提醒確認不算數
                    repositories.TASK_REMINDER_ACK_PROPERTY: {"rich_text": []},
                },
            )
        ]


class TestTaskSubmissionFallback:
    def test_finds_task_by_latest_submission_url(self, monkeypatch):
        calls = []
        expected = repositories.TaskItem(
            page_id="task-1",
            name="測試任務",
            status="待審核",
            task_type="課程時間&作業繳交",
        )

        async def fake_query(data_source_id, filter_=None):
            calls.append((data_source_id, filter_))
            return [{"id": "task-1"}]

        monkeypatch.setattr(repositories.notion_api, "query_data_source", fake_query)
        monkeypatch.setattr(repositories, "_to_task", lambda page: expected)

        result = asyncio.run(
            repositories.find_task_by_latest_submission_url(
                "https://www.notion.so/submission-1"
            )
        )

        assert result is expected
        assert calls == [
            (
                config.DS_TASKS,
                {
                    "property": "最新提交 URL",
                    "url": {"equals": "https://www.notion.so/submission-1"},
                },
            )
        ]

def slot(
    *,
    page_id: str = "slot-1",
    artist_page_ids: list[str] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    note: str = "",
):
    return repositories.AvailabilitySlot(
        page_id=page_id,
        name="Johnny｜08/05 14:00-18:00",
        artist_page_ids=artist_page_ids if artist_page_ids is not None else ["artist-1"],
        start=start,
        end=end,
        note=note,
    )


def project(
    *,
    page_id: str = "project-1",
    artist_page_ids: list[str] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    status: str | None = "進行中",
):
    return repositories.ProjectItem(
        page_id=page_id,
        name="月度排程",
        status=status,
        artist_page_ids=artist_page_ids or ["artist-1"],
        start=start,
        end=end,
    )


def task(
    *,
    page_id: str = "task-1",
    task_type: str | None = "錄音",
    course_category: str | None = None,
    status: str | None = "進行中",
    artist_page_ids: list[str] | None = None,
    project_page_ids: list[str] | None = None,
    project_types: list[str] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    start_has_time: bool = True,
    end_has_time: bool = True,
):
    return repositories.TaskItem(
        page_id=page_id,
        name="辰葳錄音",
        status=status,
        task_type=task_type,
        course_category=course_category,
        artist_page_ids=artist_page_ids if artist_page_ids is not None else ["artist-1"],
        project_page_ids=project_page_ids if project_page_ids is not None else [],
        project_types=project_types if project_types is not None else [],
        start=start,
        end=end,
        start_has_time=start_has_time,
        end_has_time=end_has_time,
    )


class TestTaskScheduleImpact:
    def test_task_directly_applies_to_artist(self):
        item = task(artist_page_ids=["artist-1"])

        assert repositories.task_applies_to_artist(item, "artist-1", {})

    def test_task_applies_to_artist_through_project(self):
        item = task(artist_page_ids=[], project_page_ids=["project-1"])
        projects = {"project-1": project(artist_page_ids=["artist-1"])}

        assert repositories.task_applies_to_artist(item, "artist-1", projects)

    def test_task_does_not_apply_to_different_artist(self):
        item = task(artist_page_ids=["artist-2"])

        assert not repositories.task_applies_to_artist(item, "artist-1", {})

    def test_completed_cancelled_and_archived_tasks_have_no_impact(self):
        for status in ("已完成", "已取消", "已封存"):
            item = task(
                status=status,
                start=datetime(2026, 8, 1, 14, 0, tzinfo=config.TZ),
                end=datetime(2026, 8, 1, 17, 0, tzinfo=config.TZ),
            )

            assert repositories.classify_task_time_impact(item) is None

    def test_recording_with_explicit_start_and_end_is_busy(self):
        item = task(
            task_type="錄音",
            start=datetime(2026, 8, 1, 14, 0, tzinfo=config.TZ),
            end=datetime(2026, 8, 1, 17, 0, tzinfo=config.TZ),
            start_has_time=True,
            end_has_time=True,
        )

        assert repositories.classify_task_time_impact(item) == "busy"

    def test_course_and_homework_session_with_explicit_time_is_busy(self):
        item = task(
            task_type="課程時間&作業繳交",
            course_category="歌唱課",
            start=datetime(2026, 8, 1, 14, 0, tzinfo=config.TZ),
            end=datetime(2026, 8, 1, 17, 0, tzinfo=config.TZ),
            start_has_time=True,
            end_has_time=True,
        )

        assert repositories.classify_task_time_impact(item) == "busy"

    def test_homework_date_range_is_warning_only(self):
        item = task(
            task_type="課程時間&作業繳交",
            course_category="歌唱課",
            start=datetime(2026, 8, 1, 0, 0, tzinfo=config.TZ),
            end=datetime(2026, 8, 3, 0, 0, tzinfo=config.TZ),
            start_has_time=False,
            end_has_time=False,
        )

        assert repositories.classify_task_time_impact(item) == "warning"

    def test_course_without_category_is_warning_only(self):
        item = task(
            task_type="課程時間&作業繳交",
            course_category=None,
            start=datetime(2026, 8, 1, 14, 0, tzinfo=config.TZ),
            end=datetime(2026, 8, 1, 17, 0, tzinfo=config.TZ),
        )

        assert repositories.classify_task_time_impact(item) == "warning"


class TestTaskProjectType:
    """M2-01：任務的分類只看 關聯專案 的 專案類型（經 Notion rollup 帶進來）。"""

    def test_reads_project_type_from_rollup(self):
        page = {
            "id": "task-1",
            "url": "",
            "properties": {
                "任務名稱": {"title": [{"plain_text": "10 月發文"}]},
                "任務狀態": {"select": {"name": "進行中"}},
                "關聯專案": {"relation": [{"id": "project-1"}]},
                "專案類型": {
                    "rollup": {
                        "type": "array",
                        "function": "show_unique",
                        "array": [
                            {"type": "select", "select": {"name": "月度排程 （發文）"}},
                        ],
                    }
                },
            },
        }

        item = repositories._to_task(page)

        assert item.project_types == ["月度排程 （發文）"]
        assert repositories.task_project_type(item) == repositories.PROJECT_TYPE_PUBLISHING

    def test_publishing_matches_only_on_exact_string(self):
        """半角空格 ＋ 全角括號。差一個字元就不該命中——這是 M2-01 的字串陷阱。

        反過來說，差一個字元若還命中，代表有人把比對改成模糊比對了。
        """
        exact = task(project_types=[repositories.PROJECT_TYPE_PUBLISHING])
        assert repositories.task_has_project_type(
            exact, repositories.PROJECT_TYPE_PUBLISHING
        )

        for wrong in (
            "月度排程（發文）",       # 少了半角空格
            "月度排程 (發文)",        # 全角括號寫成半角
            "月度排程  （發文）",     # 兩個空格
            "月度排程 （發文） ",     # 尾隨空格
            "月度排程",               # 只有前綴
        ):
            item = task(project_types=[wrong])
            assert not repositories.task_has_project_type(
                item, repositories.PROJECT_TYPE_PUBLISHING
            ), f"{wrong!r} 不該被當成發文類"

    def test_task_without_project_has_no_type(self):
        """沒關聯專案要回 None／空 list，不是炸掉——155 筆實際資料裡有 6 筆是這樣。"""
        item = task(project_page_ids=[], project_types=[])

        assert repositories.task_project_types(item) == []
        assert repositories.task_project_type(item) is None
        assert not repositories.task_has_project_type(
            item, repositories.PROJECT_TYPE_PUBLISHING
        )

    def test_missing_or_broken_rollup_degrades_to_no_type(self):
        """Notion 的 rollup 被改名或改壞時，當成「沒有分類」，不要讓 _to_task 炸掉。

        代價：M4-15／M5-03 這些靠分類的功能會靜默失效。守門測試擋不到 Notion 端
        的改動，所以 test_notion_option_values 那份選項清單要人工同步。
        """
        base = {
            "任務名稱": {"title": [{"plain_text": "無 rollup"}]},
            "任務狀態": {"select": {"name": "進行中"}},
            "關聯專案": {"relation": [{"id": "project-1"}]},
        }
        for broken in (
            {},                                                  # 欄位整個不存在
            {"專案類型": {"rollup": {"type": "array", "array": []}}},   # rollup 算不出值
            {"專案類型": {"rollup": {"type": "number", "number": 3}}},  # 被改成別的 rollup 函式
            {"專案類型": {"rollup": {"type": "array", "array": [
                {"type": "select", "select": None},
            ]}}},                                                # 關聯的專案沒填專案類型
        ):
            item = repositories._to_task({"id": "t", "url": "", "properties": {**base, **broken}})

            assert item.project_types == [], f"{broken} 應該退化成沒有分類"
            assert repositories.task_project_type(item) is None

    def test_multiple_project_types_have_no_single_type_but_still_match(self):
        """rollup 用 show_unique，關聯到兩個不同類型的專案時會回兩個值。

        那時沒有「唯一的分類」可回（硬選一個會讓行為取決於 Notion 的回傳順序），
        但「是不是某一類」的判斷仍要正確。
        """
        item = task(
            project_types=[
                repositories.PROJECT_TYPE_PUBLISHING,
                repositories.PROJECT_TYPE_WEEKLY,
            ],
        )

        assert repositories.task_project_type(item) is None
        assert repositories.task_has_project_type(
            item, repositories.PROJECT_TYPE_PUBLISHING
        )
        assert repositories.task_has_project_type(item, repositories.PROJECT_TYPE_WEEKLY)
        assert not repositories.task_has_project_type(
            item, repositories.PROJECT_TYPE_ITINERARY
        )

    def test_task_type_is_not_used_for_classification(self):
        """任務類型 管公告區與硬擋時段，不管分類（Johnny 2026-10-03 拍板）。

        所以 任務類型 寫「發布上架」但關聯專案是課程培訓時，它是課程培訓類。
        """
        item = task(
            task_type="發布上架",
            project_types=[repositories.PROJECT_TYPE_COURSE_TRAINING],
        )

        assert repositories.task_project_type(item) == repositories.PROJECT_TYPE_COURSE_TRAINING
        assert not repositories.task_has_project_type(
            item, repositories.PROJECT_TYPE_PUBLISHING
        )


class TestFinalizedSchemaMapping:
    def test_project_reads_start_date(self):
        page = {
            "id": "project-1",
            "properties": {
                "專案名稱": {"title": [{"plain_text": "八月課程"}]},
                "專案狀態": {"select": {"name": "進行中"}},
                "專案類型": {"select": {"name": "課程培訓"}},
                "關聯藝人": {"relation": []},
                "開始日": {"date": {"start": "2026-08-01"}},
                "截止日": {"date": {"start": "2026-08-31"}},
            },
        }

        item = repositories._to_project(page)

        assert item.start.date().isoformat() == "2026-08-01"

    def test_task_reads_course_meeting_relation_without_removed_nature(self):
        page = {
            "id": "task-1",
            "url": "",
            "properties": {
                "任務名稱": {"title": [{"plain_text": "歌唱課"}]},
                "任務狀態": {"select": {"name": "尚未開始"}},
                "任務類型": {"select": {"name": "課程時間&作業繳交"}},
                "課程類別": {"select": {"name": "歌唱課"}},
                "課程與會議記錄": {"relation": [{"id": "meeting-1"}]},
                "提交期限": {"date": {"start": "2026-08-20T23:59:00+08:00"}},
            },
        }

        item = repositories._to_task(page)

        assert item.course_category == "歌唱課"
        assert item.course_meeting_page_ids == ["meeting-1"]
        assert item.submission_deadline == datetime(2026, 8, 20, 23, 59, tzinfo=config.TZ)
        assert item.submission_deadline_has_time is True

    def test_task_end_falls_back_to_start_property_embedded_range(self):
        """截止日空白，但開始日本身是 Notion 原生日期範圍（同一屬性存 start+end）。"""
        page = {
            "id": "task-2",
            "url": "",
            "properties": {
                "任務名稱": {"title": [{"plain_text": "聽力訓練&樂理課-260915"}]},
                "任務狀態": {"select": {"name": "尚未開始"}},
                "任務類型": {"select": {"name": "課程時間&作業繳交"}},
                "開始日": {
                    "date": {
                        "start": "2026-09-15T13:00:00.000+08:00",
                        "end": "2026-09-15T15:00:00.000+08:00",
                    }
                },
                "截止日": {"date": None},
            },
        }

        item = repositories._to_task(page)

        assert item.start == datetime(2026, 9, 15, 13, 0, tzinfo=config.TZ)
        assert item.end == datetime(2026, 9, 15, 15, 0, tzinfo=config.TZ)
        assert item.start_has_time is True
        assert item.end_has_time is True

    def test_task_end_prefers_explicit_end_property_over_embedded_range(self):
        """截止日欄位本身有值時，不應該被開始日的內嵌 end 覆蓋。"""
        page = {
            "id": "task-3",
            "url": "",
            "properties": {
                "任務名稱": {"title": [{"plain_text": "錄音"}]},
                "任務狀態": {"select": {"name": "尚未開始"}},
                "任務類型": {"select": {"name": "錄音"}},
                "開始日": {
                    "date": {
                        "start": "2026-09-15T13:00:00.000+08:00",
                        "end": "2026-09-15T15:00:00.000+08:00",
                    }
                },
                "截止日": {"date": {"start": "2026-09-16T10:00:00.000+08:00"}},
            },
        }

        item = repositories._to_task(page)

        assert item.end == datetime(2026, 9, 16, 10, 0, tzinfo=config.TZ)

    def test_resubmission_clears_revision_deadline(self, monkeypatch):
        calls = []

        async def fake_update_page(page_id, properties):
            calls.append((page_id, properties))

        monkeypatch.setattr(repositories.notion_api, "update_page", fake_update_page)

        asyncio.run(repositories.set_task_under_review("task-1", "https://notion.so/submission"))

        assert calls == [
            ("task-1", {
                "任務狀態": {"select": {"name": "待審核"}},
                "最新提交 URL": {"url": "https://notion.so/submission"},
            }),
            ("task-1", {"修改期限": {"date": None}}),
        ]

    def test_teacher_options_include_teachers_and_company_members(self, monkeypatch):
        import asyncio

        pages = [
            {"id": "teacher", "properties": {
                "姓名 / 名稱": {"title": [{"plain_text": "阿橘"}]},
                "身份類型": {"select": {"name": "老師"}},
            }},
            {"id": "member", "properties": {
                "姓名 / 名稱": {"title": [{"plain_text": "Tony"}]},
                "身份類型": {"select": {"name": "公司成員"}},
            }},
            {"id": "vendor", "properties": {
                "姓名 / 名稱": {"title": [{"plain_text": "廠商"}]},
                "身份類型": {"select": {"name": "合作廠商"}},
            }},
        ]

        async def fake_query(*args, **kwargs):
            return pages

        monkeypatch.setattr(repositories.notion_api, "query_data_source", fake_query)

        assert asyncio.run(repositories.get_teacher_options()) == [
            ("member", "Tony"),
            ("teacher", "阿橘"),
        ]

    def test_homework_warning_applies_to_middle_day(self):
        item = task(
            task_type="課程作業",
            start=datetime(2026, 8, 1, 0, 0, tzinfo=config.TZ),
            end=datetime(2026, 8, 3, 0, 0, tzinfo=config.TZ),
            start_has_time=False,
            end_has_time=False,
        )
        warnings = repositories.warnings_for_tasks([item])

        result = repositories.warnings_for_window(
            datetime(2026, 8, 2, 14, 0, tzinfo=config.TZ),
            datetime(2026, 8, 2, 17, 0, tzinfo=config.TZ),
            warnings,
        )

        assert result == warnings


class TestAvailabilityBusySplit:
    def test_splits_availability_around_busy_interval(self):
        start = datetime(2026, 8, 1, 10, 0, tzinfo=config.TZ)
        end = datetime(2026, 8, 1, 22, 0, tzinfo=config.TZ)
        busy = [
            repositories.BusyInterval(
                source_id="task-1",
                source_name="辰葳錄音",
                source_type="Task",
                start=datetime(2026, 8, 1, 14, 0, tzinfo=config.TZ),
                end=datetime(2026, 8, 1, 17, 0, tzinfo=config.TZ),
            )
        ]

        result = repositories.split_availability_by_busy_intervals(start, end, busy)

        assert result == [
            (
                datetime(2026, 8, 1, 10, 0, tzinfo=config.TZ),
                datetime(2026, 8, 1, 14, 0, tzinfo=config.TZ),
            ),
            (
                datetime(2026, 8, 1, 17, 0, tzinfo=config.TZ),
                datetime(2026, 8, 1, 22, 0, tzinfo=config.TZ),
            ),
        ]

    def test_busy_interval_covering_whole_window_returns_no_slots(self):
        start = datetime(2026, 8, 1, 14, 0, tzinfo=config.TZ)
        end = datetime(2026, 8, 1, 17, 0, tzinfo=config.TZ)
        busy = [
            repositories.BusyInterval(
                source_id="task-1",
                source_name="辰葳錄音",
                source_type="Task",
                start=start,
                end=end,
            )
        ]

        assert repositories.split_availability_by_busy_intervals(start, end, busy) == []


class TestMonthlyPoolMatching:
    def test_monthly_pool_slot_accepts_finalized_slot(self):
        item = slot(
            start=datetime(2026, 8, 5, 14, 0, tzinfo=config.TZ),
            end=datetime(2026, 8, 5, 18, 0, tzinfo=config.TZ),
            note="收集月份：2026-08",
        )

        assert repositories.is_monthly_pool_slot(item, "2026-08")

    def test_monthly_pool_slot_matches_explicit_collection_month(self):
        item = slot(
            start=datetime(2026, 7, 31, 14, 0, tzinfo=config.TZ),
            end=datetime(2026, 7, 31, 18, 0, tzinfo=config.TZ),
            note="收集月份：2026-08",
        )

        assert repositories.availability_slot_month(item) == "2026-08"
        assert repositories.is_monthly_pool_slot(item, "2026-08")

    def test_monthly_pool_slot_falls_back_to_start_month_for_legacy_data(self):
        item = slot(
            start=datetime(2026, 8, 5, 14, 0, tzinfo=config.TZ),
            end=datetime(2026, 8, 5, 18, 0, tzinfo=config.TZ),
            note="收集性質：月度可配合時段",
        )

        assert repositories.availability_slot_month(item) == "2026-08"
        assert repositories.is_monthly_pool_slot(item, "2026-08")

    def test_busy_time_is_removed_before_common_window_matching(self):
        slots = [
            slot(
                page_id="a-slot",
                artist_page_ids=["artist-1"],
                start=datetime(2026, 8, 5, 10, 0, tzinfo=config.TZ),
                end=datetime(2026, 8, 5, 22, 0, tzinfo=config.TZ),
                note="收集月份：2026-08",
            ),
            slot(
                page_id="b-slot",
                artist_page_ids=["artist-2"],
                start=datetime(2026, 8, 5, 13, 0, tzinfo=config.TZ),
                end=datetime(2026, 8, 5, 18, 0, tzinfo=config.TZ),
                note="收集月份：2026-08",
            ),
        ]
        busy = {
            "artist-1": [
                repositories.BusyInterval(
                    source_id="task-1",
                    source_name="錄音",
                    source_type="Task",
                    start=datetime(2026, 8, 5, 14, 0, tzinfo=config.TZ),
                    end=datetime(2026, 8, 5, 17, 0, tzinfo=config.TZ),
                )
            ]
        }

        result = repositories.match_monthly_pool_windows(
            slots=slots,
            artist_page_ids=["artist-1", "artist-2"],
            month="2026-08",
            duration_minutes=60,
            busy_by_artist_page_id=busy,
        )

        assert result.missing_artist_page_ids == []
        assert result.participants["artist-1"] == [
            (
                datetime(2026, 8, 5, 10, 0, tzinfo=config.TZ),
                datetime(2026, 8, 5, 14, 0, tzinfo=config.TZ),
            ),
            (
                datetime(2026, 8, 5, 17, 0, tzinfo=config.TZ),
                datetime(2026, 8, 5, 22, 0, tzinfo=config.TZ),
            ),
        ]
        assert result.windows == [
            (
                datetime(2026, 8, 5, 13, 0, tzinfo=config.TZ),
                datetime(2026, 8, 5, 14, 0, tzinfo=config.TZ),
            ),
            (
                datetime(2026, 8, 5, 17, 0, tzinfo=config.TZ),
                datetime(2026, 8, 5, 18, 0, tzinfo=config.TZ),
            ),
        ]

    def test_missing_artist_is_reported_when_no_monthly_slots(self):
        slots = [
            slot(
                page_id="a-slot",
                artist_page_ids=["artist-1"],
                start=datetime(2026, 8, 5, 10, 0, tzinfo=config.TZ),
                end=datetime(2026, 8, 5, 22, 0, tzinfo=config.TZ),
                note="收集月份：2026-08",
            ),
        ]

        result = repositories.match_monthly_pool_windows(
            slots=slots,
            artist_page_ids=["artist-1", "artist-2"],
            month="2026-08",
            duration_minutes=60,
        )

        assert result.missing_artist_page_ids == ["artist-2"]
        assert result.windows == []

    def test_candidate_start_times_step_every_30_minutes(self):
        starts = repositories.candidate_start_times(
            datetime(2026, 8, 5, 14, 0, tzinfo=config.TZ),
            datetime(2026, 8, 5, 18, 0, tzinfo=config.TZ),
            duration_minutes=60,
        )

        assert [start.strftime("%H:%M") for start in starts] == [
            "14:00", "14:30", "15:00", "15:30", "16:00", "16:30", "17:00",
        ]

    def test_split_windows_by_duration_uses_contiguous_course_blocks(self):
        windows = repositories.split_windows_by_duration(
            [(datetime(2026, 8, 5, 12, 0, tzinfo=config.TZ),
              datetime(2026, 8, 5, 14, 0, tzinfo=config.TZ))],
            duration_minutes=60,
        )

        assert [(start.strftime("%H:%M"), end.strftime("%H:%M"))
                for start, end in windows] == [("12:00", "13:00"), ("13:00", "14:00")]

    def test_duration_display_uses_hours(self):
        assert repositories.format_duration_hours(240) == "4 小時"
        assert repositories.format_duration_hours(90) == "1.5 小時"

    def test_monthly_pool_progress_counts_each_artist_monthly_slot(self):
        slots = [
            slot(
                page_id="artist-1-pool",
                artist_page_ids=["artist-1"],
                start=datetime(2026, 8, 5, 10, 0, tzinfo=config.TZ),
                end=datetime(2026, 8, 5, 22, 0, tzinfo=config.TZ),
                note="收集月份：2026-08",
            ),
            slot(
                page_id="artist-2-pool",
                artist_page_ids=["artist-2"],
                start=datetime(2026, 8, 5, 10, 0, tzinfo=config.TZ),
                end=datetime(2026, 8, 5, 22, 0, tzinfo=config.TZ),
                note="收集月份：2026-08",
            ),
        ]

        progress = repositories.monthly_pool_progress_from_slots(
            slots,
            artist_page_ids=["artist-1", "artist-2"],
            month="2026-08",
        )

        assert progress.filled_artist_page_ids == ["artist-1", "artist-2"]
        assert progress.missing_artist_page_ids == []
        assert progress.is_complete


class TestDuplicateAvailabilitySlots:
    def test_finds_duplicate_pool_slot_for_same_artist_and_window(self):
        start = datetime(2026, 8, 5, 14, 0, tzinfo=config.TZ)
        end = datetime(2026, 8, 5, 18, 0, tzinfo=config.TZ)
        slots = [slot(start=start, end=end)]

        duplicate = repositories.find_duplicate_availability_slot(
            slots,
            artist_page_id="artist-1",
            start=start,
            end=end,
        )

        assert duplicate == slots[0]

    def test_ignores_same_time_from_different_artist(self):
        start = datetime(2026, 8, 5, 14, 0, tzinfo=config.TZ)
        end = datetime(2026, 8, 5, 18, 0, tzinfo=config.TZ)
        slots = [slot(artist_page_ids=["artist-2"], start=start, end=end)]

        assert repositories.find_duplicate_availability_slot(
            slots,
            artist_page_id="artist-1",
            start=start,
            end=end,
        ) is None

class TestCompactAvailabilitySlots:
    def test_archives_exact_duplicate_and_keeps_one_visible_slot(self):
        start = datetime(2026, 8, 1, 13, 30, tzinfo=config.TZ)
        end = datetime(2026, 8, 1, 22, 0, tzinfo=config.TZ)
        slots = [
            slot(page_id="slot-1", start=start, end=end, note="收集性質：待確認"),
            slot(page_id="slot-2", start=start, end=end, note="收集性質：待確認"),
        ]

        result = repositories.compact_availability_slots(slots)

        assert [slot.page_id for slot in result.visible] == ["slot-1"]
        assert [slot.page_id for slot in result.redundant] == ["slot-2"]

    def test_keeps_long_legacy_slot_over_contained_legacy_slot(self):
        slots = [
            slot(
                page_id="legacy-long",
                start=datetime(2026, 8, 1, 13, 0, tzinfo=config.TZ),
                end=datetime(2026, 8, 1, 22, 0, tzinfo=config.TZ),
                note="收集性質：可直接安排",
            ),
            slot(
                page_id="confirm-short",
                start=datetime(2026, 8, 1, 13, 30, tzinfo=config.TZ),
                end=datetime(2026, 8, 1, 17, 0, tzinfo=config.TZ),
                note="收集性質：待確認",
            ),
        ]

        result = repositories.compact_availability_slots(slots)

        assert [slot.page_id for slot in result.visible] == ["legacy-long"]
        assert [slot.page_id for slot in result.redundant] == ["confirm-short"]

    def test_keeps_later_end_when_same_start_and_same_nature(self):
        slots = [
            slot(
                page_id="short",
                start=datetime(2026, 8, 1, 13, 30, tzinfo=config.TZ),
                end=datetime(2026, 8, 1, 17, 0, tzinfo=config.TZ),
                note="收集性質：待確認",
            ),
            slot(
                page_id="long",
                start=datetime(2026, 8, 1, 13, 30, tzinfo=config.TZ),
                end=datetime(2026, 8, 1, 22, 0, tzinfo=config.TZ),
                note="收集性質：待確認",
            ),
        ]

        result = repositories.compact_availability_slots(slots)

        assert [slot.page_id for slot in result.visible] == ["long"]
        assert [slot.page_id for slot in result.redundant] == ["short"]

    def test_long_legacy_slot_replaces_contained_legacy_slot_without_priority(self):
        slots = [
            slot(
                page_id="confirm-long",
                start=datetime(2026, 8, 1, 10, 0, tzinfo=config.TZ),
                end=datetime(2026, 8, 1, 22, 0, tzinfo=config.TZ),
                note="收集性質：待確認",
            ),
            slot(
                page_id="direct-short",
                start=datetime(2026, 8, 1, 13, 30, tzinfo=config.TZ),
                end=datetime(2026, 8, 1, 17, 0, tzinfo=config.TZ),
                note="收集性質：可直接安排",
            ),
        ]

        result = repositories.compact_availability_slots(slots)

        assert [slot.page_id for slot in result.visible] == ["confirm-long"]
        assert [slot.page_id for slot in result.redundant] == ["direct-short"]

    def test_merges_overlapping_same_day_same_nature_to_earliest_start_and_latest_end(self):
        slots = [
            slot(
                page_id="early",
                start=datetime(2026, 8, 1, 10, 0, tzinfo=config.TZ),
                end=datetime(2026, 8, 1, 17, 0, tzinfo=config.TZ),
                note="收集性質：月度可配合時段",
            ),
            slot(
                page_id="late",
                start=datetime(2026, 8, 1, 13, 0, tzinfo=config.TZ),
                end=datetime(2026, 8, 1, 22, 0, tzinfo=config.TZ),
                note="收集性質：月度可配合時段",
            ),
        ]

        result = repositories.compact_availability_slots(slots)

        assert [slot.page_id for slot in result.visible] == ["early"]
        assert result.visible[0].start == datetime(2026, 8, 1, 10, 0, tzinfo=config.TZ)
        assert result.visible[0].end == datetime(2026, 8, 1, 22, 0, tzinfo=config.TZ)
        assert [slot.page_id for slot in result.redundant] == ["late"]
        assert result.updates == [
            repositories.AvailabilitySlotWindowUpdate(
                page_id="early",
                start=datetime(2026, 8, 1, 10, 0, tzinfo=config.TZ),
                end=datetime(2026, 8, 1, 22, 0, tzinfo=config.TZ),
            )
        ]

    def test_does_not_merge_separate_windows_on_same_day(self):
        slots = [
            slot(
                page_id="morning",
                start=datetime(2026, 8, 1, 10, 0, tzinfo=config.TZ),
                end=datetime(2026, 8, 1, 12, 0, tzinfo=config.TZ),
                note="收集性質：可直接安排",
            ),
            slot(
                page_id="night",
                start=datetime(2026, 8, 1, 19, 0, tzinfo=config.TZ),
                end=datetime(2026, 8, 1, 22, 0, tzinfo=config.TZ),
                note="收集性質：可直接安排",
            ),
        ]

        result = repositories.compact_availability_slots(slots)

        assert [slot.page_id for slot in result.visible] == ["morning", "night"]
        assert result.redundant == []
        assert result.updates == []

    def test_does_not_merge_slots_from_different_artists(self):
        slots = [
            slot(
                page_id="artist-1-slot",
                artist_page_ids=["artist-1"],
                start=datetime(2026, 8, 1, 10, 0, tzinfo=config.TZ),
                end=datetime(2026, 8, 1, 17, 0, tzinfo=config.TZ),
                note="收集性質：可直接安排",
            ),
            slot(
                page_id="artist-2-slot",
                artist_page_ids=["artist-2"],
                start=datetime(2026, 8, 1, 13, 0, tzinfo=config.TZ),
                end=datetime(2026, 8, 1, 22, 0, tzinfo=config.TZ),
                note="收集性質：可直接安排",
            ),
        ]

        result = repositories.compact_availability_slots(slots)

        assert [slot.page_id for slot in result.visible] == ["artist-1-slot", "artist-2-slot"]
        assert result.redundant == []
        assert result.updates == []

    def test_does_not_merge_slots_from_different_dates(self):
        slots = [
            slot(
                page_id="day-1",
                start=datetime(2026, 8, 1, 13, 0, tzinfo=config.TZ),
                end=datetime(2026, 8, 1, 18, 0, tzinfo=config.TZ),
                note="收集性質：月度可配合時段",
            ),
            slot(
                page_id="day-2",
                start=datetime(2026, 8, 2, 13, 0, tzinfo=config.TZ),
                end=datetime(2026, 8, 2, 18, 0, tzinfo=config.TZ),
                note="收集性質：月度可配合時段",
            ),
        ]

        result = repositories.compact_availability_slots(slots)

        assert [item.page_id for item in result.visible] == ["day-1", "day-2"]
        assert result.redundant == []
        assert result.updates == []

class TestScheduleImpactContextCache:
    def test_context_uses_only_task_and_project_sources(self, monkeypatch):
        import asyncio

        async def fake_query(data_source_id, filter_=None, sorts=None):
            return []

        monkeypatch.setattr(repositories.notion_api, "query_data_source", fake_query)
        repositories._invalidate_impact_context_cache()

        context = asyncio.run(
            repositories.get_schedule_impact_context_for_artist(
                "artist-without-course-sessions", use_cache=False
            )
        )

        assert context.busy_intervals == []
        assert context.warnings == []

    def test_caches_within_ttl_and_refetches_after_invalidate(self, monkeypatch):
        import asyncio

        calls = {"n": 0}

        async def fake_query(data_source_id, filter_=None, sorts=None):
            calls["n"] += 1
            return []

        monkeypatch.setattr(repositories.notion_api, "query_data_source", fake_query)
        repositories._invalidate_impact_context_cache()

        asyncio.run(repositories.get_schedule_impact_context_for_artist("artist-cache-1"))
        first = calls["n"]
        assert first > 0  # 第一次真的打了 Notion（含全表掃描）

        asyncio.run(repositories.get_schedule_impact_context_for_artist("artist-cache-1"))
        assert calls["n"] == first  # TTL 內命中快取，完全沒再打 Notion

        repositories._invalidate_impact_context_cache()
        asyncio.run(repositories.get_schedule_impact_context_for_artist("artist-cache-1"))
        assert calls["n"] > first  # 失效後會重新查

    def test_use_cache_false_bypasses_cache(self, monkeypatch):
        import asyncio

        calls = {"n": 0}

        async def fake_query(data_source_id, filter_=None, sorts=None):
            calls["n"] += 1
            return []

        monkeypatch.setattr(repositories.notion_api, "query_data_source", fake_query)
        repositories._invalidate_impact_context_cache()

        asyncio.run(repositories.get_schedule_impact_context_for_artist("artist-cache-2"))
        first = calls["n"]
        asyncio.run(
            repositories.get_schedule_impact_context_for_artist("artist-cache-2", use_cache=False)
        )
        assert calls["n"] > first  # 明確 bypass 時仍會重新查


class TestArtistOnlySeesDispatchedTasks:
    """藝人填時段時不得看到未勾「Discord 顯示」的草稿（會議 2026-09-12 決議）。

    未派發的草稿連任務名稱都會出現在「已排除與正式安排衝突的時間」提示裡，
    藝人因此知道有一筆還沒對外的安排。控方端相反——比對共同空檔時必須把草稿
    算成已佔用，否則會把排掉的時段當空檔推出去。
    """

    @staticmethod
    def _task_page(page_id: str, name: str, *, discord_visible: bool, project_id: str = ""):
        return {
            "id": page_id,
            "url": f"https://www.notion.so/{page_id}",
            "properties": {
                "任務名稱": {"title": [{"plain_text": name}]},
                "任務狀態": {"select": {"name": "進行中"}},
                # 「錄音」在 HARD_BLOCK_TASK_TYPES 裡，配上完整起訖時間才會算成 busy
                # （＝真的把時段擋掉並顯示任務名稱），這正是外洩草稿名稱的那條路徑。
                "任務類型": {"select": {"name": "錄音"}},
                "Discord 顯示": {"checkbox": discord_visible},
                "開始日": {"date": {"start": "2026-09-17T19:30:00+08:00",
                                  "end": "2026-09-17T21:30:00+08:00"}},
                "截止日": {"date": None},
                "關聯藝人": {"relation": []},
                "關聯專案": {"relation": [{"id": project_id}] if project_id else []},
            },
        }

    def _run(self, monkeypatch, *, discord_visible_only):
        import asyncio

        seen_filters = []
        dispatched = self._task_page("t-open", "已派發課程", discord_visible=True,
                                     project_id="proj-1")
        draft = self._task_page("t-draft", "還沒派發的草稿", discord_visible=False,
                                project_id="proj-1")

        async def fake_query(data_source_id, filter_=None, sorts=None):
            seen_filters.append(filter_)
            if data_source_id != config.DS_TASKS:
                return []
            if filter_ is None:
                return [dispatched, draft]
            # 模擬 Notion 端就篩掉未勾選的任務
            return [dispatched]

        async def fake_project_map():
            return {"proj-1": repositories.ProjectItem(
                page_id="proj-1", name="九月培訓", status="進行中",
                artist_page_ids=["artist-1"],
            )}

        monkeypatch.setattr(repositories.notion_api, "query_data_source", fake_query)
        monkeypatch.setattr(repositories, "get_project_map", fake_project_map)
        monkeypatch.setattr(repositories, "get_projects_for_artist", _no_projects)
        repositories._invalidate_impact_context_cache()

        context = asyncio.run(repositories.get_schedule_impact_context_for_artist(
            "artist-1", use_cache=False, discord_visible_only=discord_visible_only
        ))
        return context, seen_filters

    def test_artist_side_hides_undispatched_drafts(self, monkeypatch):
        context, seen_filters = self._run(monkeypatch, discord_visible_only=True)

        names = [item.source_name for item in context.busy_intervals]
        assert names == ["已派發課程"]
        assert "還沒派發的草稿" not in names
        # 在 Notion 端就篩掉，不是撈全表再過濾（鐵則 3）
        assert any(f is not None for f in seen_filters)

    def test_admin_side_still_counts_drafts_as_busy(self, monkeypatch):
        context, _ = self._run(monkeypatch, discord_visible_only=False)

        names = sorted(item.source_name for item in context.busy_intervals)
        assert names == ["已派發課程", "還沒派發的草稿"]

    def test_indirect_project_relation_survives_the_filter(self, monkeypatch):
        """換成只認直接關聯的查詢會把這種任務弄丟，撞課提示就會漏。"""
        context, _ = self._run(monkeypatch, discord_visible_only=True)

        # 任務的「關聯藝人」是空的，只靠 關聯專案 → 專案的藝人才對應得上
        assert [item.source_name for item in context.busy_intervals] == ["已派發課程"]

    def test_artist_and_admin_caches_do_not_poison_each_other(self, monkeypatch):
        """兩邊共用 artist page id 當 key 的話，30 秒內先跑的那邊會餵給另一邊。"""
        import asyncio

        dispatched = self._task_page("t-open", "已派發課程", discord_visible=True)
        draft = self._task_page("t-draft", "還沒派發的草稿", discord_visible=False)

        async def fake_query(data_source_id, filter_=None, sorts=None):
            if data_source_id != config.DS_TASKS:
                return []
            return [dispatched, draft] if filter_ is None else [dispatched]

        async def fake_project_map():
            return {}

        monkeypatch.setattr(repositories.notion_api, "query_data_source", fake_query)
        monkeypatch.setattr(repositories, "get_project_map", fake_project_map)
        monkeypatch.setattr(repositories, "get_projects_for_artist", _no_projects)
        monkeypatch.setattr(repositories, "task_applies_to_artist",
                            lambda *_args, **_kwargs: True)
        repositories._invalidate_impact_context_cache()

        # 控方先跑並寫進快取，藝人端緊接著在 TTL 內查詢
        admin = asyncio.run(repositories.get_schedule_impact_context_for_artist("artist-1"))
        artist = asyncio.run(repositories.get_schedule_impact_context_for_artist(
            "artist-1", discord_visible_only=True
        ))

        assert len(admin.busy_intervals) == 2
        assert [item.source_name for item in artist.busy_intervals] == ["已派發課程"]

    def test_invalidate_clears_both_variants_for_one_artist(self, monkeypatch):
        import asyncio

        calls = {"n": 0}

        async def fake_query(data_source_id, filter_=None, sorts=None):
            calls["n"] += 1
            return []

        monkeypatch.setattr(repositories.notion_api, "query_data_source", fake_query)
        repositories._invalidate_impact_context_cache()

        asyncio.run(repositories.get_schedule_impact_context_for_artist("artist-9"))
        asyncio.run(repositories.get_schedule_impact_context_for_artist(
            "artist-9", discord_visible_only=True))
        warmed = calls["n"]

        repositories._invalidate_impact_context_cache("artist-9")
        asyncio.run(repositories.get_schedule_impact_context_for_artist("artist-9"))
        asyncio.run(repositories.get_schedule_impact_context_for_artist(
            "artist-9", discord_visible_only=True))

        assert calls["n"] > warmed  # 兩種變體都被清掉，不會有一邊繼續吃舊快取


class TestDiscordDisplayQueryFallback:
    def test_falls_back_to_legacy_property_name_only_for_that_error(self, monkeypatch):
        import asyncio

        used = []

        async def fake_query(data_source_id, filter_=None, sorts=None):
            used.append(filter_)
            if len(used) == 1:
                raise repositories.NotionAPIError(400, "Discord 顯示 is not a property that exists")
            return []

        monkeypatch.setattr(repositories.notion_api, "query_data_source", fake_query)

        asyncio.run(repositories._query_tasks_requiring_discord_display())

        assert used[0]["property"] == "Discord 顯示"
        assert used[1]["property"] == "Discord 可見"

    def test_other_notion_errors_are_not_swallowed(self, monkeypatch):
        import asyncio

        async def fake_query(data_source_id, filter_=None, sorts=None):
            raise repositories.NotionAPIError(401, "unauthorized")

        monkeypatch.setattr(repositories.notion_api, "query_data_source", fake_query)

        with pytest.raises(repositories.NotionAPIError):
            asyncio.run(repositories._query_tasks_requiring_discord_display())


class TestFutureOnlyQueries:
    def test_future_date_filter_uses_today_lower_bound(self):
        filter_ = repositories._future_date_filter("可用結束時間")

        assert filter_["property"] == "可用結束時間"
        assert filter_["date"]["on_or_after"] == datetime.now(config.TZ).date().isoformat()

    def test_get_slots_for_artist_bounds_query_to_future(self, monkeypatch):
        import asyncio

        captured: dict = {}

        async def fake_query(data_source_id, filter_=None, sorts=None):
            captured["filter"] = filter_
            return []

        monkeypatch.setattr(repositories.notion_api, "query_data_source", fake_query)

        asyncio.run(repositories.get_slots_for_artist("artist-1"))
        conditions = captured["filter"]["and"]
        assert {"property": "填寫藝人", "relation": {"contains": "artist-1"}} in conditions
        assert any("date" in c for c in conditions)  # 未來時段的天級粗篩

        asyncio.run(repositories.get_slots_for_artist("artist-1", future_only=False))
        assert captured["filter"] == {"property": "填寫藝人", "relation": {"contains": "artist-1"}}


class TestTaskDiscordDisplayProperty:
    def test_reads_new_discord_display_checkbox_name(self):
        page = {
            "id": "task-1",
            "url": "",
            "properties": {
                "任務名稱": {"title": [{"plain_text": "錄音"}]},
                "Discord 顯示": {"checkbox": True},
            },
        }

        assert repositories._task_discord_visible(page) is True

    def test_falls_back_to_legacy_discord_visible_checkbox_name(self):
        page = {
            "id": "task-1",
            "url": "",
            "properties": {
                "任務名稱": {"title": [{"plain_text": "錄音"}]},
                "Discord 可見": {"checkbox": True},
            },
        }

        assert repositories._task_discord_visible(page) is True

    def test_prefers_new_discord_display_checkbox_name(self):
        page = {
            "id": "task-1",
            "url": "",
            "properties": {
                "任務名稱": {"title": [{"plain_text": "錄音"}]},
                "Discord 顯示": {"checkbox": False},
                "Discord 可見": {"checkbox": True},
            },
        }

        assert repositories._task_discord_visible(page) is False


def display_task(
    name: str = "任務",
    status: str | None = "進行中",
    end: datetime | None = None,
    end_has_time: bool = False,
    task_type: str | None = None,
):
    return repositories.TaskItem(
        page_id=f"task-{name}",
        name=name,
        status=status,
        task_type=task_type,
        end=end,
        end_has_time=end_has_time,
    )


NOW = datetime(2026, 7, 8, 12, 0, tzinfo=config.TZ)


class TestTaskDeadlines:
    def test_open_submission_prefers_submission_deadline_over_course_end(self):
        task = display_task(
            end=datetime(2026, 7, 8, 13, 0, tzinfo=config.TZ),
            end_has_time=True,
        )
        task.open_for_submission = True
        task.submission_deadline = datetime(2026, 7, 10, 23, 59, tzinfo=config.TZ)
        task.submission_deadline_has_time = True

        deadline, has_time = repositories.task_action_deadline(task)

        assert deadline == datetime(2026, 7, 10, 23, 59, tzinfo=config.TZ)
        assert has_time is True
        assert repositories.task_deadline_passed(task, NOW) is False

    def test_needs_fix_uses_revision_flow_instead_of_submission_deadline(self):
        task = display_task(
            status="需修改",
            end=datetime(2026, 7, 1, tzinfo=config.TZ),
        )
        task.open_for_submission = True
        task.submission_deadline = datetime(2026, 7, 2, tzinfo=config.TZ)

        assert repositories.task_action_deadline(task) == (None, False)
        assert repositories.task_deadline_passed(task, NOW) is False
        assert repositories.compute_due_reminder_stages(task, NOW) == []

    def test_date_only_deadline_valid_through_the_day(self):
        t = display_task(end=datetime(2026, 7, 8, 0, 0, tzinfo=config.TZ), end_has_time=False)
        assert repositories.task_deadline_passed(t, NOW) is False

    def test_date_only_deadline_passed_next_day(self):
        t = display_task(end=datetime(2026, 7, 7, 0, 0, tzinfo=config.TZ), end_has_time=False)
        assert repositories.task_deadline_passed(t, NOW) is True

    def test_datetime_deadline_compares_exact_time(self):
        t = display_task(end=datetime(2026, 7, 8, 11, 0, tzinfo=config.TZ), end_has_time=True)
        assert repositories.task_deadline_passed(t, NOW) is True

    def test_due_within_three_days(self):
        soon = display_task(end=datetime(2026, 7, 10, 0, 0, tzinfo=config.TZ))
        far = display_task(end=datetime(2026, 7, 20, 0, 0, tzinfo=config.TZ))
        assert repositories.task_due_within(soon, NOW) is True
        assert repositories.task_due_within(far, NOW) is False


class TestClassifyTasksForDisplay:
    def test_buckets_are_mutually_exclusive_with_expected_precedence(self):
        tasks = [
            display_task("已交", status="待審核", end=datetime(2026, 7, 1, tzinfo=config.TZ)),  # 過期但已交 → in_review
            display_task("過期", status="進行中", end=datetime(2026, 7, 1, tzinfo=config.TZ)),
            display_task("標記逾期", status="已逾期"),
            display_task("要修", status="需修改"),
            display_task("快到", status="待提交", end=datetime(2026, 7, 9, tzinfo=config.TZ)),
            display_task("普通", status="尚未開始", end=datetime(2026, 8, 1, tzinfo=config.TZ)),
            display_task("完成", status="已完成"),
            display_task("封存", status="已封存"),
        ]

        buckets = repositories.classify_tasks_for_display(tasks, NOW)

        assert [t.name for t in buckets["in_review"]] == ["已交"]
        assert {t.name for t in buckets["overdue"]} == {"過期", "標記逾期"}
        assert [t.name for t in buckets["needs_fix"]] == ["要修"]
        assert [t.name for t in buckets["due_soon"]] == ["快到"]
        assert [t.name for t in buckets["open"]] == ["普通"]

    def test_sorts_by_deadline_with_none_last(self):
        tasks = [
            display_task("無期限", status="進行中"),
            display_task("晚", status="進行中", end=datetime(2026, 8, 20, tzinfo=config.TZ)),
            display_task("早", status="進行中", end=datetime(2026, 8, 1, tzinfo=config.TZ)),
        ]

        buckets = repositories.classify_tasks_for_display(tasks, NOW)

        assert [t.name for t in buckets["open"]] == ["早", "晚", "無期限"]


def reminder_task(rules: list[str], end: datetime | None, status: str = "進行中"):
    t = display_task(status=status, end=end)
    t.reminder_rules = rules
    return t


class TestComputeDueReminderStages:
    """提醒階段計算。

    除了逾期與待審核那幾條（會提早 return，不碰規則），**每個測試都明確傳
    `default_rules`**：預設值與勾選值自 2026-10-03 起是聯集，不傳就會被 .env
    影響，測試會因為本機設定不同而變色。
    """

    def test_respects_configured_rules_only(self):
        end = datetime(2026, 7, 11, tzinfo=config.TZ)  # 3 天後
        with_rule = reminder_task(["3 天前"], end)
        without_rule = reminder_task(["當天"], end)

        assert repositories.compute_due_reminder_stages(
            with_rule, NOW, default_rules=()) == ["3 天前"]
        assert repositories.compute_due_reminder_stages(
            without_rule, NOW, default_rules=()) == []

    def test_catchup_fires_missed_stage(self):
        end = datetime(2026, 7, 9, tzinfo=config.TZ)  # 剩 1 天，但規則是 3 天前
        t = reminder_task(["3 天前"], end)

        assert repositories.compute_due_reminder_stages(t, NOW, default_rules=()) == ["3 天前"]

    def test_five_days_before_rule(self):
        """客戶要的「截止前 5 天提醒」；沿用既有機制，只是多一個規則值。"""
        not_yet = reminder_task(["5 天前"], datetime(2026, 7, 14, tzinfo=config.TZ))  # 剩 6 天
        due = reminder_task(["5 天前"], datetime(2026, 7, 13, tzinfo=config.TZ))      # 剩 5 天

        assert repositories.compute_due_reminder_stages(not_yet, NOW, default_rules=()) == []
        assert repositories.compute_due_reminder_stages(due, NOW, default_rules=()) == ["5 天前"]

    def test_multiple_rules_return_far_to_near(self):
        """同時勾多個規則時，回傳順序由遠到近，訊息才不會倒著念。"""
        t = reminder_task(["當天", "5 天前", "1 天前"], datetime(2026, 7, 8, tzinfo=config.TZ))

        assert repositories.compute_due_reminder_stages(
            t, NOW, default_rules=()) == ["5 天前", "1 天前", "當天"]

    def test_empty_rules_fall_back_to_default(self):
        """控方沒勾任何規則時，套用租戶預設，不必逐筆維護。"""
        not_yet = reminder_task([], datetime(2026, 7, 14, tzinfo=config.TZ))  # 剩 6 天
        due = reminder_task([], datetime(2026, 7, 13, tzinfo=config.TZ))      # 剩 5 天

        assert repositories.compute_due_reminder_stages(not_yet, NOW, default_rules=("5 天前",)) == []
        assert repositories.compute_due_reminder_stages(due, NOW, default_rules=("5 天前",)) == ["5 天前"]

    def test_tenant_default_is_d5_and_d3(self):
        """租戶預設＝D-5 ＋ D-3（09/22 會議「兩個節點並行」，2026-10-03 對齊）。

        這條釘住的是商業決定，不是程式細節。要改預設就要同時改 `.env`、
        `.env.example`、`config.py` 的 fallback 與這個測試，四處一致。
        """
        assert config.get_tenant().default_reminder_rules == ("5 天前", "3 天前")

    def test_tenant_default_fires_both_nodes(self):
        """D-5 與 D-3 各觸發一次，不是只發最近的那個。"""
        default = ("5 天前", "3 天前")
        six_days = reminder_task([], datetime(2026, 7, 14, tzinfo=config.TZ))  # 剩 6 天
        five_days = reminder_task([], datetime(2026, 7, 13, tzinfo=config.TZ))  # 剩 5 天
        three_days = reminder_task([], datetime(2026, 7, 11, tzinfo=config.TZ))  # 剩 3 天

        assert repositories.compute_due_reminder_stages(six_days, NOW, default_rules=default) == []
        assert repositories.compute_due_reminder_stages(
            five_days, NOW, default_rules=default) == ["5 天前"]
        # 進入 D-3 後兩個階段都算「該觸發」；實際只各發一次，由呼叫端的 _once 去重
        assert repositories.compute_due_reminder_stages(
            three_days, NOW, default_rules=default) == ["5 天前", "3 天前"]

    def test_explicit_rules_are_unioned_with_default(self):
        """勾選與預設是聯集（Johnny 2026-10-03 拍板）。

        勾選只能往上加，不能減少。這條以前是反的（替換式），改動理由見
        compute_due_reminder_stages 的 docstring。
        """
        t = reminder_task(["當天"], datetime(2026, 7, 13, tzinfo=config.TZ))  # 剩 5 天

        assert repositories.compute_due_reminder_stages(
            t, NOW, default_rules=("5 天前",)) == ["5 天前"]

    def test_checking_a_rule_cannot_reduce_reminders(self):
        """疊加的代價：控方無法用勾選把提醒收斂掉。

        勾「當天」不會關掉預設的 D-5。這是刻意的取捨，不是 bug —— 想減少
        提醒只能改租戶預設值。
        """
        default = ("5 天前", "3 天前")
        t = reminder_task(["當天"], datetime(2026, 7, 11, tzinfo=config.TZ))  # 剩 3 天

        stages = repositories.compute_due_reminder_stages(t, NOW, default_rules=default)

        assert stages == ["5 天前", "3 天前"]  # 當天還沒到，但預設的兩個都在

    def test_default_rules_can_be_disabled(self):
        """租戶把預設設成空的時候，回到「沒勾就不提醒」的舊行為。"""
        t = reminder_task([], datetime(2026, 7, 13, tzinfo=config.TZ))

        assert repositories.compute_due_reminder_stages(t, NOW, default_rules=()) == []

    def test_overdue_only_rule_no_longer_opts_out(self):
        """只勾「逾期」不再能關閉事前提醒 —— 疊加之後預設照樣生效。

        改成疊加前，「逾期」讓 reminder_rules 變成非空、因而擋掉預設，是控方
        關閉事前提醒的唯一方式。現在預設與勾選聯集，這個手法失效了：剩 5 天
        的任務即使只勾「逾期」，仍會收到預設的 D-5。
        """
        before = reminder_task(["逾期"], datetime(2026, 7, 13, tzinfo=config.TZ))  # 剩 5 天
        after = reminder_task(["逾期"], datetime(2026, 7, 1, tzinfo=config.TZ))    # 已逾期

        assert repositories.compute_due_reminder_stages(
            before, NOW, default_rules=("5 天前",)) == ["5 天前"]
        # 預設為空時才回到「事前不吵」
        assert repositories.compute_due_reminder_stages(before, NOW, default_rules=()) == []
        assert repositories.compute_due_reminder_stages(after, NOW) == [repositories.OVERDUE_STAGE]

    def test_overdue_fires_regardless_of_rules(self):
        t = reminder_task([], datetime(2026, 7, 1, tzinfo=config.TZ))

        assert repositories.compute_due_reminder_stages(t, NOW) == [repositories.OVERDUE_STAGE]

    def test_under_review_tasks_are_not_reminded(self):
        """東西交出去了，球在公司手上；9/23 交了 9/25 還被催「還有 5 天」會被當成 bug。"""
        due = reminder_task(["當天"], datetime(2026, 7, 8, tzinfo=config.TZ), status="待審核")
        overdue = reminder_task([], datetime(2026, 7, 1, tzinfo=config.TZ), status="待審核")

        assert repositories.compute_due_reminder_stages(due, NOW) == []
        assert repositories.compute_due_reminder_stages(overdue, NOW) == []


class TestApprovalSubmissionDeadline:
    """「需審批」＝ T-5 交件死線（會議 2026-09-12）。

    控方勾了就走：提交期限有填用它，沒填用截止日往前推 approval_lead_days。
    沒勾（例如上課）完全不受影響。
    """

    def test_unchecked_task_has_no_approval_deadline(self):
        task = display_task(end=datetime(2026, 9, 30, tzinfo=config.TZ))

        assert repositories.approval_submission_deadline(task) == (None, False)
        assert repositories.task_action_deadline(task)[0] == task.end
        assert repositories.task_deadline_kind(task) == "截止"

    def test_checked_without_submission_deadline_backs_off_from_end_date(self):
        task = display_task(end=datetime(2026, 9, 30, tzinfo=config.TZ))
        task.needs_approval = True

        deadline, has_time = repositories.approval_submission_deadline(task, lead_days=5)

        assert deadline == datetime(2026, 9, 25, tzinfo=config.TZ)
        assert has_time is False
        assert repositories.task_deadline_kind(task) == "交件"

    def test_checked_with_explicit_submission_deadline_uses_it(self):
        """控方要客製天數就填提交期限，它優先於自動回推。"""
        task = display_task(end=datetime(2026, 9, 30, tzinfo=config.TZ))
        task.needs_approval = True
        task.submission_deadline = datetime(2026, 9, 27, tzinfo=config.TZ)

        deadline, _ = repositories.task_action_deadline(task)

        assert deadline == datetime(2026, 9, 27, tzinfo=config.TZ)

    def test_approval_deadline_drives_overdue_not_end_date(self):
        """9/25 沒交就是逾期，不用等到 9/30——這是「要判定逾期、通知控方」那個決定。"""
        task = display_task(end=datetime(2026, 9, 30, tzinfo=config.TZ))
        task.needs_approval = True
        sept_26 = datetime(2026, 9, 26, 9, 0, tzinfo=config.TZ)

        assert repositories.task_deadline_passed(task, sept_26) is True
        assert repositories.compute_due_reminder_stages(
            task, sept_26, default_rules=("當天",)
        ) == [repositories.OVERDUE_STAGE]

    def test_same_day_reminder_lands_on_approval_deadline(self):
        """預設「當天」提醒發在交件日 9/25，正好是李葳要的「截止前 5 天發一個提醒」。"""
        task = display_task(end=datetime(2026, 9, 30, tzinfo=config.TZ))
        task.needs_approval = True
        sept_25 = datetime(2026, 9, 25, 9, 0, tzinfo=config.TZ)
        sept_24 = datetime(2026, 9, 24, 9, 0, tzinfo=config.TZ)

        assert repositories.compute_due_reminder_stages(task, sept_24, default_rules=("當天",)) == []
        assert repositories.compute_due_reminder_stages(task, sept_25, default_rules=("當天",)) == ["當天"]

    def test_checked_without_any_date_is_harmless(self):
        task = display_task(end=None)
        task.needs_approval = True

        assert repositories.approval_submission_deadline(task) == (None, False)
        assert repositories.compute_due_reminder_stages(task, NOW) == []


class TestReminderAckMerge:
    def test_appends_new_line_and_keeps_order(self):
        merged = repositories.merge_reminder_ack_line(
            "當天｜09/25 09:00｜已發送給 3 人", "當天｜09/25 09:12｜Johnny"
        )

        assert merged == "當天｜09/25 09:00｜已發送給 3 人\n當天｜09/25 09:12｜Johnny"

    def test_duplicate_line_returns_none_so_notion_is_not_rewritten(self):
        """同一位藝人連按兩下只留一筆，也不多打一次 Notion。"""
        existing = "當天｜09/25 09:12｜Johnny"

        assert repositories.merge_reminder_ack_line(existing, "當天｜09/25 09:12｜Johnny") is None

    def test_blank_line_is_ignored(self):
        assert repositories.merge_reminder_ack_line("x", "   ") is None

    def test_append_degrades_when_property_missing(self, monkeypatch):
        """舊 workspace 還沒加「提醒確認」欄位：按鈕照樣能用，紀錄留在 Audit Log。"""
        async def fake_retrieve(page_id):
            return {"id": page_id, "properties": {}}

        async def fake_update(page_id, props):
            raise repositories.NotionAPIError(400, f"{repositories.TASK_REMINDER_ACK_PROPERTY} is not a property that exists")

        monkeypatch.setattr(repositories.notion_api, "retrieve_page", fake_retrieve)
        monkeypatch.setattr(repositories.notion_api, "update_page", fake_update)

        stored = asyncio.run(repositories.append_reminder_ack_line("task-1", "當天｜09/25 09:12｜Johnny"))

        assert stored is False

    def test_append_reraises_unrelated_notion_errors(self, monkeypatch):
        async def fake_retrieve(page_id):
            return {"id": page_id, "properties": {}}

        async def fake_update(page_id, props):
            raise repositories.NotionAPIError(401, "unauthorized")

        monkeypatch.setattr(repositories.notion_api, "retrieve_page", fake_retrieve)
        monkeypatch.setattr(repositories.notion_api, "update_page", fake_update)

        with pytest.raises(repositories.NotionAPIError):
            asyncio.run(repositories.append_reminder_ack_line("task-1", "當天｜09/25 09:12｜Johnny"))

    def test_append_writes_merged_text(self, monkeypatch):
        writes = []

        async def fake_retrieve(page_id):
            return {"id": page_id, "properties": {
                repositories.TASK_REMINDER_ACK_PROPERTY: {"rich_text": [{"plain_text": "當天｜09/25 09:00｜已發送給 2 人"}]},
            }}

        async def fake_update(page_id, props):
            writes.append(props)

        monkeypatch.setattr(repositories.notion_api, "retrieve_page", fake_retrieve)
        monkeypatch.setattr(repositories.notion_api, "update_page", fake_update)

        stored = asyncio.run(repositories.append_reminder_ack_line("task-1", "當天｜09/25 09:12｜Johnny"))

        assert stored is True
        assert writes[0][repositories.TASK_REMINDER_ACK_PROPERTY]["rich_text"][0]["text"]["content"] == (
            "當天｜09/25 09:00｜已發送給 2 人\n當天｜09/25 09:12｜Johnny"
        )

    def test_resend_reset_clears_reminder_ack_too(self, monkeypatch):
        """重送代表內容或時間改了，舊的確認不算數。"""
        writes = []

        async def fake_update(page_id, props):
            writes.append(props)

        monkeypatch.setattr(repositories.notion_api, "update_page", fake_update)

        asyncio.run(repositories.reset_task_discord_notification("task-1"))

        assert writes[0][repositories.TASK_REMINDER_ACK_PROPERTY] == {"rich_text": []}
        assert writes[0]["Discord 收到時間"] == {"date": None}

    def test_resend_reset_degrades_when_property_missing(self, monkeypatch):
        calls = []

        async def fake_update(page_id, props):
            calls.append(props)
            if repositories.TASK_REMINDER_ACK_PROPERTY in props:
                raise repositories.NotionAPIError(400, f"{repositories.TASK_REMINDER_ACK_PROPERTY} is not a property that exists")

        monkeypatch.setattr(repositories.notion_api, "update_page", fake_update)

        asyncio.run(repositories.reset_task_discord_notification("task-1"))

        assert repositories.TASK_REMINDER_ACK_PROPERTY not in calls[-1]
        assert calls[-1]["Discord 通知時間"] == {"date": None}

    def test_inactive_or_no_deadline_returns_nothing(self):
        done = reminder_task(["當天"], datetime(2026, 7, 8, tzinfo=config.TZ), status="已完成")
        no_end = reminder_task(["當天"], None)

        assert repositories.compute_due_reminder_stages(done, NOW) == []
        assert repositories.compute_due_reminder_stages(no_end, NOW) == []


class TestReviewDeadlines:
    def test_revision_deadline_only_applies_to_needs_fix(self):
        task = display_task(status="需修改")
        task.revision_deadline = NOW + repositories.timedelta(hours=12)
        assert repositories.compute_revision_deadline_stage(task, NOW) == "修改期限將到"
        task.status = "進行中"
        assert repositories.compute_revision_deadline_stage(task, NOW) is None


class TestCourseMeetingHiddenTypes:
    """會議秘書小律：排除清單內的會議類型永遠不讀取（見 QA_HIDDEN_MEETING_TYPES）；
    其餘類型（含未來新增的任何類型）預設開放，只靠「藝人可見」逐筆把關。"""

    def test_filter_excludes_configured_hidden_types_only(self, monkeypatch):
        captured: dict = {}

        async def fake_query(data_source_id, filter_=None, sorts=None):
            if data_source_id == config.DS_COURSE_MEETINGS:
                captured["filter"] = filter_
            return []

        monkeypatch.setattr(repositories.notion_api, "query_data_source", fake_query)
        fake_tenant = dataclasses.replace(
            config.get_tenant(),
            qa_hidden_meeting_types=("課程", "課程會議", "公司例會", "管理層會議"),
        )
        monkeypatch.setattr(repositories.config, "get_tenant", lambda: fake_tenant)

        asyncio.run(repositories.get_course_meetings_for_artist("artist-1"))

        conditions = captured["filter"]["and"]
        assert {"property": "藝人可見", "checkbox": {"equals": True}} in conditions
        excluded = {
            c["select"]["does_not_equal"]
            for c in conditions
            if "select" in c and "does_not_equal" in c["select"]
        }
        assert excluded == {"課程", "課程會議", "公司例會", "管理層會議"}
        # 沒有被寫死排除的類型（例如藝人單人會議、企劃會議，或客戶未來新增的任何類型）
        # 不會出現在條件裡——代表它們預設放行，只靠「藝人可見」把關。
        assert "藝人單人會議" not in excluded
        assert "企劃會議" not in excluded

    def test_empty_hidden_list_still_queries_with_visibility_check_only(self, monkeypatch):
        calls = {"n": 0}
        captured: dict = {}

        async def fake_query(data_source_id, filter_=None, sorts=None):
            calls["n"] += 1
            if data_source_id == config.DS_COURSE_MEETINGS:
                captured["filter"] = filter_
            return []

        monkeypatch.setattr(repositories.notion_api, "query_data_source", fake_query)
        fake_tenant = dataclasses.replace(config.get_tenant(), qa_hidden_meeting_types=())
        monkeypatch.setattr(repositories.config, "get_tenant", lambda: fake_tenant)

        result = asyncio.run(repositories.get_course_meetings_for_artist("artist-1"))

        assert result == []
        assert calls["n"] > 0
        assert captured["filter"] == {
            "and": [{"property": "藝人可見", "checkbox": {"equals": True}}]
        }


class TestDynamicSelectOptions:
    """任務類型／課程類別即時讀 Notion schema，不寫死在 Bot 裡。"""

    def test_get_task_type_options_reads_live_schema(self, monkeypatch):
        async def fake_retrieve(data_source_id):
            assert data_source_id == config.DS_TASKS
            return {
                "properties": {
                    "任務類型": {
                        "select": {"options": [{"name": "通告"}, {"name": "課程時間&作業繳交"}, {"name": " "}]}
                    }
                }
            }

        monkeypatch.setattr(repositories.notion_api, "retrieve_data_source", fake_retrieve)

        assert asyncio.run(repositories.get_task_type_options()) == ["通告", "課程時間&作業繳交"]

    def test_get_course_type_options_reads_live_schema(self, monkeypatch):
        async def fake_retrieve(data_source_id):
            assert data_source_id == config.DS_TASKS
            return {
                "properties": {
                    "課程類別": {
                        "select": {"options": [{"name": "歌唱課"}, {"name": "編曲課"}]}
                    }
                }
            }

        monkeypatch.setattr(repositories.notion_api, "retrieve_data_source", fake_retrieve)

        assert asyncio.run(repositories.get_course_type_options()) == ["歌唱課", "編曲課"]

    def test_get_course_type_options_missing_property_returns_empty(self, monkeypatch):
        async def fake_retrieve(data_source_id):
            return {"properties": {}}

        monkeypatch.setattr(repositories.notion_api, "retrieve_data_source", fake_retrieve)

        assert asyncio.run(repositories.get_course_type_options()) == []
