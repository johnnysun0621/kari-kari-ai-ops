"""time_collection.py helper tests."""

from datetime import date, datetime

import config
from services import time_collection, timeparse


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

        assert "每頁約一半月份" in sop
        assert "下一頁" in sop
        assert "不同時間就分批選" in sop
        assert "整天可：08:00–24:00" in sop
        assert len(sop) < 220

    def test_page_hint_changes_by_selection_state(self):
        assert "先勾日期" in time_collection.availability_page_hint(0, None)
        assert "再選時間" in time_collection.availability_page_hint(2, None)
        assert "送出" in time_collection.availability_page_hint(2, "after")

    def test_announcement_copy_is_concise(self):
        message = time_collection.availability_announcement_copy("2026-08")

        assert "Kari 行政安排課程、錄音或通告" in message
        assert "Kari 行政會再向你確認" in message
        assert "填寫方式" not in message
        assert "整天可" not in message
        assert "選擇可配合時段" in message
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

        assert [len(page) for page in pages] == [16, 15]
        assert pages[0][0].value == "2026-08-01"
        assert pages[0][-1].value == "2026-08-16"
        assert pages[1][0].value == "2026-08-17"
        assert pages[1][-1].value == "2026-08-31"

    def test_month_pages_are_balanced_for_different_month_lengths(self):
        for month, expected in (("2026-02", [14, 14]), ("2026-04", [15, 15]), ("2026-08", [16, 15])):
            weeks = [week.value for week in time_collection.month_week_options(month)]
            assert [len(page) for page in time_collection.day_option_pages(weeks)] == expected


class TestBuildSlot:
    def test_all_day_uses_0800_to_midnight(self):
        start, end = time_collection.build_mode_slot("2026-08-05", "all_day")

        assert start == datetime(2026, 8, 5, 8, 0, tzinfo=config.TZ)
        assert end == datetime(2026, 8, 6, 0, 0, tzinfo=config.TZ)
        assert timeparse.humanize(start, end) == "8/5（週三）08:00–24:00"

    def test_builds_datetime_from_day_and_preset(self):
        start, end = time_collection.build_slot("2026-08-05", "14:00-18:00")

        assert start == datetime(2026, 8, 5, 14, 0, tzinfo=config.TZ)
        assert end == datetime(2026, 8, 5, 18, 0, tzinfo=config.TZ)

    def test_builds_cross_product_for_multiple_days_and_presets(self):
        slots = time_collection.build_slots(
            ["2026-08-05", "2026-08-06"],
            ["08:00-12:00", "19:00-24:00"],
        )

        assert len(slots) == 4
        assert slots[0] == (
            datetime(2026, 8, 5, 8, 0, tzinfo=config.TZ),
            datetime(2026, 8, 5, 12, 0, tzinfo=config.TZ),
        )
        assert slots[-1] == (
            datetime(2026, 8, 6, 19, 0, tzinfo=config.TZ),
            datetime(2026, 8, 7, 0, 0, tzinfo=config.TZ),
        )


class TestBuildModeSlot:
    def test_all_day_uses_operating_window(self):
        start, end = time_collection.build_mode_slot("2026-08-05", "all_day")

        assert start == datetime(2026, 8, 5, 8, 0, tzinfo=config.TZ)
        assert end == datetime(2026, 8, 6, 0, 0, tzinfo=config.TZ)

    def test_after_uses_custom_start_and_default_end(self):
        start, end = time_collection.build_mode_slot("2026-08-05", "after", start_text="13:30")

        assert start == datetime(2026, 8, 5, 13, 30, tzinfo=config.TZ)
        assert end == datetime(2026, 8, 6, 0, 0, tzinfo=config.TZ)

    def test_before_uses_default_start_and_custom_end(self):
        start, end = time_collection.build_mode_slot("2026-08-05", "before", end_text="17:00")

        assert start == datetime(2026, 8, 5, 8, 0, tzinfo=config.TZ)
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


class TestActiveCollectionPersistence:
    def test_collections_survive_restart(self):
        deadline = datetime(2026, 8, 1, 12, 0, tzinfo=config.TZ)
        collection = time_collection.register_active_collection(
            "2026-08", deadline, ["artist-1", "artist-2"]
        )

        time_collection.reload_state_for_tests()  # 模擬 bot 重啟

        assert time_collection.active_collections() == [collection]

    def test_sent_notices_survive_restart(self):
        assert time_collection.collection_notice_once("2026-08", 123, "pre_deadline")

        time_collection.reload_state_for_tests()  # 模擬 bot 重啟

        assert not time_collection.collection_notice_once("2026-08", 123, "pre_deadline")
        assert time_collection.collection_notice_once("2026-08", 123, "post_deadline")

    def test_deregister_removes_collection_across_restart(self):
        deadline = datetime(2026, 8, 1, 12, 0, tzinfo=config.TZ)
        collection = time_collection.register_active_collection(
            "2026-08", deadline, ["artist-1"]
        )

        time_collection.deregister_collection(collection.month, collection.deadline_ts)
        time_collection.reload_state_for_tests()

        assert time_collection.active_collections() == []

    def test_stale_months_are_pruned_on_load(self):
        old_deadline = datetime(2025, 1, 5, 12, 0, tzinfo=config.TZ)
        current = time_collection.register_active_collection(
            "2099-01", datetime(2099, 1, 5, tzinfo=config.TZ), ["artist-1"]
        )
        time_collection.register_active_collection("2025-01", old_deadline, ["artist-1"])

        time_collection.reload_state_for_tests()

        assert time_collection.active_collections() == [current]

    def test_corrupt_state_file_is_ignored(self):
        time_collection._STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        time_collection._STATE_PATH.write_text("not json", encoding="utf-8")

        time_collection.reload_state_for_tests()

        assert time_collection.active_collections() == []  # 壞檔不炸、當空狀態
