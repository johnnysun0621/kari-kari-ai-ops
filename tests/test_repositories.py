"""repositories.py pure helper tests."""

from datetime import datetime

import config
from services import repositories


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
            artist("Paused", discord_user_id="123", status="暫停"),
            artist("Archived", discord_user_id="456", status="封存"),
        ]

        assert repositories.filter_schedulable_artists(artists) == []

    def test_keeps_artist_with_discord_id_and_empty_status(self):
        artists = [artist("A", discord_user_id="123", status=None)]

        assert repositories.filter_schedulable_artists(artists) == artists


class TestAvailabilitySlotProps:
    def test_pool_slot_omits_schedule_request_relation(self):
        props = repositories._build_availability_slot_props(
            request_page_id=None,
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

    def test_request_slot_keeps_schedule_request_relation(self):
        props = repositories._build_availability_slot_props(
            request_page_id="request-1",
            artist=artist("Johnny"),
            start=datetime(2026, 8, 5, 14, 0, tzinfo=config.TZ),
            end=datetime(2026, 8, 5, 18, 0, tzinfo=config.TZ),
            is_late=False,
        )

        assert props["關聯排課需求"]["relation"] == [{"id": "request-1"}]


def slot(
    *,
    page_id: str = "slot-1",
    request_page_ids: list[str] | None = None,
    artist_page_ids: list[str] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    note: str = "",
):
    return repositories.AvailabilitySlot(
        page_id=page_id,
        name="Johnny｜08/05 14:00-18:00",
        request_page_ids=request_page_ids or [],
        artist_page_ids=artist_page_ids if artist_page_ids is not None else ["artist-1"],
        start=start,
        end=end,
        note=note,
    )


def session(
    *,
    page_id: str = "session-1",
    artist_page_ids: list[str] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    status: str | None = "已排定",
):
    return repositories.CourseSession(
        page_id=page_id,
        name="例會",
        status=status,
        artist_page_ids=artist_page_ids or ["artist-1"],
        start=start,
        end=end,
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
    course_item_nature: str | None = None,
    status: str | None = "進行中",
    artist_page_ids: list[str] | None = None,
    project_page_ids: list[str] | None = None,
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
        course_item_nature=course_item_nature,
        artist_page_ids=artist_page_ids if artist_page_ids is not None else ["artist-1"],
        project_page_ids=project_page_ids if project_page_ids is not None else [],
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
            task_type="課程與作業",
            course_item_nature="課程場次",
            start=datetime(2026, 8, 1, 14, 0, tzinfo=config.TZ),
            end=datetime(2026, 8, 1, 17, 0, tzinfo=config.TZ),
            start_has_time=True,
            end_has_time=True,
        )

        assert repositories.classify_task_time_impact(item) == "busy"

    def test_homework_date_range_is_warning_only(self):
        item = task(
            task_type="課程與作業",
            course_item_nature="課程作業",
            start=datetime(2026, 8, 1, 0, 0, tzinfo=config.TZ),
            end=datetime(2026, 8, 3, 0, 0, tzinfo=config.TZ),
            start_has_time=False,
            end_has_time=False,
        )

        assert repositories.classify_task_time_impact(item) == "warning"

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
    def test_monthly_pool_slot_excludes_schedule_request_slots(self):
        item = slot(
            request_page_ids=["request-1"],
            start=datetime(2026, 8, 5, 14, 0, tzinfo=config.TZ),
            end=datetime(2026, 8, 5, 18, 0, tzinfo=config.TZ),
            note="收集月份：2026-08",
        )

        assert not repositories.is_monthly_pool_slot(item, "2026-08")

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

    def test_monthly_pool_progress_counts_only_pool_slots(self):
        slots = [
            slot(
                page_id="artist-1-pool",
                artist_page_ids=["artist-1"],
                start=datetime(2026, 8, 5, 10, 0, tzinfo=config.TZ),
                end=datetime(2026, 8, 5, 22, 0, tzinfo=config.TZ),
                note="收集月份：2026-08",
            ),
            slot(
                page_id="artist-2-request",
                artist_page_ids=["artist-2"],
                request_page_ids=["request-1"],
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

        assert progress.filled_artist_page_ids == ["artist-1"]
        assert progress.missing_artist_page_ids == ["artist-2"]
        assert not progress.is_complete


class TestCourseSessionOccupiedDates:
    def test_returns_future_occupied_dates_for_artist(self):
        sessions = [
            session(
                artist_page_ids=["artist-1"],
                start=datetime(2026, 8, 5, 14, 0, tzinfo=config.TZ),
                end=datetime(2026, 8, 5, 15, 0, tzinfo=config.TZ),
            ),
            session(
                artist_page_ids=["artist-2"],
                start=datetime(2026, 8, 6, 14, 0, tzinfo=config.TZ),
                end=datetime(2026, 8, 6, 15, 0, tzinfo=config.TZ),
            ),
        ]

        result = repositories.occupied_session_dates_for_artist(
            sessions,
            artist_page_id="artist-1",
            now=datetime(2026, 8, 1, 0, 0, tzinfo=config.TZ),
        )

        assert result == {datetime(2026, 8, 5, tzinfo=config.TZ).date()}

    def test_ignores_cancelled_and_past_sessions(self):
        sessions = [
            session(
                page_id="cancelled",
                status="已取消",
                start=datetime(2026, 8, 5, 14, 0, tzinfo=config.TZ),
                end=datetime(2026, 8, 5, 15, 0, tzinfo=config.TZ),
            ),
            session(
                page_id="past",
                start=datetime(2026, 7, 1, 14, 0, tzinfo=config.TZ),
                end=datetime(2026, 7, 1, 15, 0, tzinfo=config.TZ),
            ),
        ]

        result = repositories.occupied_session_dates_for_artist(
            sessions,
            artist_page_id="artist-1",
            now=datetime(2026, 8, 1, 0, 0, tzinfo=config.TZ),
        )

        assert result == set()


class TestCourseSessionBusyIntervals:
    def test_course_session_with_precise_start_and_end_is_busy(self):
        sessions = [
            session(
                page_id="session-1",
                start=datetime(2026, 8, 5, 14, 0, tzinfo=config.TZ),
                end=datetime(2026, 8, 5, 15, 0, tzinfo=config.TZ),
            )
        ]

        result = repositories.busy_intervals_for_course_sessions(sessions)

        assert result == [
            repositories.BusyInterval(
                source_id="session-1",
                source_name="例會",
                source_type="CourseSession",
                start=datetime(2026, 8, 5, 14, 0, tzinfo=config.TZ),
                end=datetime(2026, 8, 5, 15, 0, tzinfo=config.TZ),
            )
        ]

    def test_course_session_without_end_is_not_precise_busy_time(self):
        sessions = [
            session(
                start=datetime(2026, 8, 5, 14, 0, tzinfo=config.TZ),
                end=None,
            )
        ]

        assert repositories.busy_intervals_for_course_sessions(sessions) == []


class TestDuplicateAvailabilitySlots:
    def test_finds_duplicate_pool_slot_for_same_artist_and_window(self):
        start = datetime(2026, 8, 5, 14, 0, tzinfo=config.TZ)
        end = datetime(2026, 8, 5, 18, 0, tzinfo=config.TZ)
        slots = [slot(start=start, end=end)]

        duplicate = repositories.find_duplicate_availability_slot(
            slots,
            request_page_id=None,
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
            request_page_id=None,
            artist_page_id="artist-1",
            start=start,
            end=end,
        ) is None

    def test_request_slots_only_match_same_request(self):
        start = datetime(2026, 8, 5, 14, 0, tzinfo=config.TZ)
        end = datetime(2026, 8, 5, 18, 0, tzinfo=config.TZ)
        slots = [slot(request_page_ids=["request-2"], start=start, end=end)]

        assert repositories.find_duplicate_availability_slot(
            slots,
            request_page_id="request-1",
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


class TestScheduleImpactContextCache:
    def test_context_does_not_query_deleted_course_sessions_data_source(self, monkeypatch):
        import asyncio

        async def fake_query(data_source_id, filter_=None, sorts=None):
            return []

        async def deleted_course_sessions_query(*args, **kwargs):
            raise AssertionError("deleted Course Sessions data source must not be queried")

        monkeypatch.setattr(repositories.notion_api, "query_data_source", fake_query)
        monkeypatch.setattr(repositories, "get_course_sessions_for_artist", deleted_course_sessions_query)
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
    def test_respects_configured_rules_only(self):
        end = datetime(2026, 7, 11, tzinfo=config.TZ)  # 3 天後
        with_rule = reminder_task(["3 天前"], end)
        without_rule = reminder_task(["當天"], end)

        assert repositories.compute_due_reminder_stages(with_rule, NOW) == ["3 天前"]
        assert repositories.compute_due_reminder_stages(without_rule, NOW) == []

    def test_catchup_fires_missed_stage(self):
        end = datetime(2026, 7, 9, tzinfo=config.TZ)  # 剩 1 天，但規則是 3 天前
        t = reminder_task(["3 天前"], end)

        assert repositories.compute_due_reminder_stages(t, NOW) == ["3 天前"]

    def test_overdue_fires_regardless_of_rules(self):
        t = reminder_task([], datetime(2026, 7, 1, tzinfo=config.TZ))

        assert repositories.compute_due_reminder_stages(t, NOW) == [repositories.OVERDUE_STAGE]

    def test_inactive_or_no_deadline_returns_nothing(self):
        done = reminder_task(["當天"], datetime(2026, 7, 8, tzinfo=config.TZ), status="已完成")
        no_end = reminder_task(["當天"], None)

        assert repositories.compute_due_reminder_stages(done, NOW) == []
        assert repositories.compute_due_reminder_stages(no_end, NOW) == []
