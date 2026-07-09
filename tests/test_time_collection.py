"""time_collection.py helper tests."""

from datetime import date, datetime

import config
from services import time_collection


class TestCollectionNature:
    def test_labels_collection_natures(self):
        assert time_collection.DEFAULT_COLLECTION_NATURE == "monthly_reference"
        assert time_collection.collection_nature_label("monthly_reference") == "月度可配合時段"

    def test_legacy_collection_nature_values_map_to_monthly_reference(self):
        assert time_collection.collection_nature_label("direct") == "月度可配合時段"
        assert time_collection.collection_nature_label("confirm") == "月度可配合時段"

    def test_rejects_unknown_collection_nature(self):
        try:
            time_collection.collection_nature_label("unknown")
        except ValueError as exc:
            assert "Unknown collection nature" in str(exc)
        else:
            raise AssertionError("expected ValueError")


class TestAvailabilitySelectionSop:
    def test_sop_is_short_and_user_oriented(self):
        sop = time_collection.availability_selection_sop()

        assert "每頁顯示 7 天" in sop
        assert "一次" in sop
        assert "重複勾同一天" in sop
        assert len(sop) < 220

    def test_page_hint_changes_by_selection_state(self):
        assert "先勾日期" in time_collection.availability_page_hint(0, None)
        assert "再選時間" in time_collection.availability_page_hint(2, None)
        assert "送出" in time_collection.availability_page_hint(2, "after")

    def test_announcement_copy_is_concise(self):
        message = time_collection.availability_announcement_copy("2026-08")

        assert "2026-08 可配合時間" in message
        assert "不是正式預約" in message
        assert len(message) < 500


class TestSlotDisplay:
    def test_extracts_collection_nature_from_note(self):
        note = "收集月份：2026-08｜收集性質：可直接安排｜藝人備註：可錄音"

        assert time_collection.note_field(note, "收集性質") == "可直接安排"
        assert time_collection.note_field(note, "藝人備註") == "可錄音"

    def test_formats_slot_summary_with_weekday_and_nature(self):
        summary = time_collection.format_slot_summary(
            datetime(2026, 8, 5, 14, 0, tzinfo=config.TZ),
            datetime(2026, 8, 5, 18, 0, tzinfo=config.TZ),
            note="收集月份：2026-08｜收集性質：月度可配合時段",
        )

        assert summary == "8/5（週三） 14:00-18:00｜月度可配合時段"

    def test_formats_legacy_slot_summary_as_monthly_reference(self):
        summary = time_collection.format_slot_summary(
            datetime(2026, 8, 5, 14, 0, tzinfo=config.TZ),
            datetime(2026, 8, 5, 18, 0, tzinfo=config.TZ),
            note="收集月份：2026-08｜收集性質：待確認",
        )

        assert summary == "8/5（週三） 14:00-18:00｜月度可配合時段"


class TestNormalizeMonth:
    def test_accepts_short_month(self):
        assert time_collection.normalize_month("2026-8") == "2026-08"

    def test_rejects_invalid_month(self):
        assert time_collection.normalize_month("2026-13") is None
        assert time_collection.normalize_month("August") is None


class TestMonthWeekOptions:
    def test_splits_month_into_week_chunks(self):
        options = time_collection.month_week_options("2026-08")

        assert options[0].label == "8/1-8/7"
        assert options[0].value == "2026-08-01:2026-08-07"
        assert options[-1].label == "8/29-8/31"


class TestWeekDayOptions:
    def test_builds_day_options_for_week(self):
        options = time_collection.week_day_options("2026-08-01:2026-08-07")

        assert options[0].label == "8/1（週六）"
        assert options[0].value == "2026-08-01"
        assert options[-1].label == "8/7（週五）"

    def test_combines_day_options_for_multiple_weeks(self):
        options = time_collection.week_values_day_options([
            "2026-08-01:2026-08-07",
            "2026-08-08:2026-08-14",
        ])

        assert len(options) == 14
        assert options[0].value == "2026-08-01"
        assert options[-1].value == "2026-08-14"

    def test_excludes_blocked_dates_from_week_options(self):
        options = time_collection.week_values_day_options(
            ["2026-08-01:2026-08-07"],
            excluded_dates={date(2026, 8, 3), date(2026, 8, 5)},
        )

        assert [option.value for option in options] == [
            "2026-08-01",
            "2026-08-02",
            "2026-08-04",
            "2026-08-06",
            "2026-08-07",
        ]

    def test_splits_day_options_into_discord_sized_pages(self):
        weeks = [week.value for week in time_collection.month_week_options("2026-08")]

        pages = time_collection.day_option_pages(weeks)

        assert [len(page) for page in pages] == [25, 6]
        assert pages[0][0].value == "2026-08-01"
        assert pages[0][-1].value == "2026-08-25"
        assert pages[1][0].value == "2026-08-26"
        assert pages[1][-1].value == "2026-08-31"


class TestBuildSlot:
    def test_builds_datetime_from_day_and_preset(self):
        start, end = time_collection.build_slot("2026-08-05", "14:00-18:00")

        assert start == datetime(2026, 8, 5, 14, 0, tzinfo=config.TZ)
        assert end == datetime(2026, 8, 5, 18, 0, tzinfo=config.TZ)

    def test_builds_cross_product_for_multiple_days_and_presets(self):
        slots = time_collection.build_slots(
            ["2026-08-05", "2026-08-06"],
            ["10:00-12:00", "19:00-22:00"],
        )

        assert len(slots) == 4
        assert slots[0] == (
            datetime(2026, 8, 5, 10, 0, tzinfo=config.TZ),
            datetime(2026, 8, 5, 12, 0, tzinfo=config.TZ),
        )
        assert slots[-1] == (
            datetime(2026, 8, 6, 19, 0, tzinfo=config.TZ),
            datetime(2026, 8, 6, 22, 0, tzinfo=config.TZ),
        )


class TestBuildModeSlot:
    def test_all_day_uses_operating_window(self):
        start, end = time_collection.build_mode_slot("2026-08-05", "all_day")

        assert start == datetime(2026, 8, 5, 10, 0, tzinfo=config.TZ)
        assert end == datetime(2026, 8, 5, 22, 0, tzinfo=config.TZ)

    def test_after_uses_custom_start_and_default_end(self):
        start, end = time_collection.build_mode_slot("2026-08-05", "after", start_text="13:30")

        assert start == datetime(2026, 8, 5, 13, 30, tzinfo=config.TZ)
        assert end == datetime(2026, 8, 5, 22, 0, tzinfo=config.TZ)

    def test_before_uses_default_start_and_custom_end(self):
        start, end = time_collection.build_mode_slot("2026-08-05", "before", end_text="17:00")

        assert start == datetime(2026, 8, 5, 10, 0, tzinfo=config.TZ)
        assert end == datetime(2026, 8, 5, 17, 0, tzinfo=config.TZ)

    def test_specific_uses_custom_range(self):
        start, end = time_collection.build_mode_slot(
            "2026-08-05", "specific", start_text="14:00", end_text="18:00"
        )

        assert start == datetime(2026, 8, 5, 14, 0, tzinfo=config.TZ)
        assert end == datetime(2026, 8, 5, 18, 0, tzinfo=config.TZ)


class TestActiveCollectionRegistry:
    def test_registers_active_monthly_collection(self):
        time_collection.clear_active_collections_for_tests()
        deadline = datetime(2026, 8, 1, 12, 0, tzinfo=config.TZ)

        collection = time_collection.register_active_collection(
            "2026-8",
            deadline,
            ["artist-1", "artist-2"],
        )

        assert collection.month == "2026-08"
        assert collection.deadline_ts == int(deadline.timestamp())
        assert collection.artist_page_ids == ("artist-1", "artist-2")
        assert time_collection.active_collections() == [collection]

    def test_collection_notice_once_deduplicates_stages(self):
        time_collection.clear_active_collections_for_tests()

        assert time_collection.collection_notice_once("2026-08", 123, "all_done")
        assert not time_collection.collection_notice_once("2026-08", 123, "all_done")
        assert time_collection.collection_notice_once("2026-08", 123, "pre_deadline")

    def test_rejects_invalid_active_collection_month(self):
        time_collection.clear_active_collections_for_tests()

        try:
            time_collection.register_active_collection(
                "bad-month",
                datetime(2026, 8, 1, 12, 0, tzinfo=config.TZ),
                ["artist-1"],
            )
        except ValueError as exc:
            assert "Invalid month" in str(exc)
        else:
            raise AssertionError("expected ValueError")
