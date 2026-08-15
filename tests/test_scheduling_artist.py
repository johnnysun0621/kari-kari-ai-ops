"""Discord artist scheduling UI helper tests."""

from datetime import date, datetime
import inspect

from cogs import scheduling_artist
import discord

import config
from cogs.scheduling_artist import (
    AvailabilityConflictConfirmView,
    AvailabilityBatchConfirmView,
    AvailabilityConfirmView,
    AvailabilityDraftGroup,
    AvailabilityDraftManageView,
    DatePresetView,
    PoolContinueView,
    SchedulingArtistCog,
)
from services import repositories, time_collection


def child_labels(view):
    return [getattr(child, "label", None) for child in view.children]


class TestDatePresetView:
    def test_dates_before_today_are_not_displayed(self):
        week_values = [week.value for week in time_collection.month_week_options("2026-08")]
        view = DatePresetView(
            "2026-08",
            deadline_ts=1785600000,
            week_values=week_values,
            nature_code=time_collection.DEFAULT_COLLECTION_NATURE,
            today=date(2026, 8, 15),
        )

        visible_values = [option.value for page in view.day_pages for option in page]
        assert visible_values[0] == "2026-08-15"
        assert visible_values[-1] == "2026-08-31"
        assert "2026-08-14" not in visible_values
        assert [len(page) for page in view.day_pages] == [9, 8]

    def test_month_dates_are_shown_as_discord_sized_select_pages(self):
        week_values = [week.value for week in time_collection.month_week_options("2026-08")]
        view = DatePresetView(
            "2026-08",
            deadline_ts=1785600000,
            week_values=week_values,
            nature_code=time_collection.DEFAULT_COLLECTION_NATURE,
            today=date(2026, 8, 1),
        )

        assert len(view.all_day_values) == 31
        assert [len(page) for page in view.day_pages] == [16, 15]
        selects = [child for child in view.children if isinstance(child, discord.ui.Select)]
        assert len(selects) == 1
        assert selects[0].max_values == 16
        assert [option.label for option in selects[0].options][:2] == ["8/1（週六）", "8/2（週日）"]
        labels = child_labels(view)
        assert "下一頁" in labels
        assert "整天可（08:00–24:00）" in labels
        assert "指定時段" in labels
        assert "取消" in labels
        assert "選全部日期" not in labels
        assert "本頁：8/1–8/16" in view.prompt()
        assert "尚未選日期" in view.prompt()
        assert "填寫方式" in view.prompt()
        assert "整天可：08:00–24:00" in view.prompt()


def test_availability_management_view_has_cancel_navigation():
    artist = repositories.Artist(
        page_id="artist-1", name="Johnny", discord_user_id="123", status="已簽約"
    )
    view = scheduling_artist.ManageAvailabilityView(
        "2026-08", 1785600000, time_collection.DEFAULT_COLLECTION_NATURE,
        artist, [], "",
    )

    assert "取消" in child_labels(view)


def test_month_overview_allows_artist_to_choose_a_month_or_cancel():
    artist = repositories.Artist(
        page_id="artist-1", name="Johnny", discord_user_id="123", status="已簽約"
    )
    slot = repositories.AvailabilitySlot(
        page_id="slot-1",
        name="8/12 可配合",
        start=datetime(2026, 8, 12, 8, 0, tzinfo=config.TZ),
        end=datetime(2026, 8, 12, 12, 0, tzinfo=config.TZ),
        artist_page_ids=[artist.page_id],
    )
    view = scheduling_artist.AvailabilityMonthOverviewView(
        None,
        artist,
        [slot],
        datetime(2026, 8, 1, tzinfo=config.TZ),
    )

    labels = child_labels(view)
    assert "取消" in labels
    selects = [child for child in view.children if isinstance(child, discord.ui.Select)]
    assert len(selects) == 1
    assert any(option.value == "2026-08" for option in selects[0].options)


class TestDatePresetInteractions:
    def test_draft_groups_accumulate_until_final_submit(self):
        first = AvailabilityDraftGroup(
            day_values=("2026-08-15", "2026-08-16"),
            mode="all_day",
        )
        second = AvailabilityDraftGroup(
            day_values=("2026-08-20",),
            mode="specific",
            start_text="13:00",
            end_text="15:00",
        )

        slots = scheduling_artist._draft_slots([first, second])

        assert len(slots) == 3
        assert slots[0][0] == datetime(2026, 8, 15, 8, 0, tzinfo=config.TZ)
        assert slots[-1] == (
            datetime(2026, 8, 20, 13, 0, tzinfo=config.TZ),
            datetime(2026, 8, 20, 15, 0, tzinfo=config.TZ),
        )
        assert scheduling_artist._draft_day_values([first, second]) == {
            "2026-08-15",
            "2026-08-16",
            "2026-08-20",
        }

    def test_draft_is_resumed_by_user_and_month_even_if_entry_deadline_changes(self):
        group = AvailabilityDraftGroup(("2026-08-15",), "all_day")
        scheduling_artist._set_drafts(123, "2026-08", 1000, [group])

        assert scheduling_artist._drafts_for(123, "2026-08", 2000) == [group]

        scheduling_artist._set_drafts(123, "2026-08", 2000, [])

    def test_group_confirmation_adds_to_draft_instead_of_submitting(self):
        week_values = [week.value for week in time_collection.month_week_options("2026-08")]
        view = AvailabilityConfirmView(
            "2026-08",
            1785600000,
            time_collection.DEFAULT_COLLECTION_NATURE,
            week_values,
            ["2026-08-15"],
            "all_day",
        )

        assert "加入本次回報" in child_labels(view)
        assert "確認送出" not in child_labels(view)
        assert "不會立即寫入 Notion" in view.prompt()

    def test_continue_view_exposes_draft_management_and_final_review(self):
        view = PoolContinueView(
            "2026-08",
            1785600000,
            time_collection.DEFAULT_COLLECTION_NATURE,
            [week.value for week in time_collection.month_week_options("2026-08")],
            draft_count=2,
        )

        labels = child_labels(view)
        assert "繼續選日期" in labels
        assert "查看／移除已選" in labels
        assert "檢查全部時段" in labels

    def test_draft_management_pages_more_than_twenty_five_groups(self):
        groups = [
            AvailabilityDraftGroup((f"2026-08-{day:02d}",), "all_day")
            for day in range(1, 32)
        ]
        first = AvailabilityDraftManageView(
            "2026-08",
            1785600000,
            time_collection.DEFAULT_COLLECTION_NATURE,
            [],
            groups,
            "",
        )
        second = first.page_view(1)

        assert "頁面 1/2" in first.prompt()
        assert "下一頁" in child_labels(first)
        assert "頁面 2/2" in second.prompt()
        assert len(second.select.options) == 6

    def test_last_day_button_page_shows_remaining_month_days(self):
        week_values = [week.value for week in time_collection.month_week_options("2026-08")]
        view = DatePresetView(
            "2026-08",
            deadline_ts=1785600000,
            week_values=week_values,
            nature_code=time_collection.DEFAULT_COLLECTION_NATURE,
            page_index=1,
            today=date(2026, 8, 1),
        )

        selects = [child for child in view.children if isinstance(child, discord.ui.Select)]
        assert [option.label for option in selects[0].options][0] == "8/17（週一）"
        assert [option.label for option in selects[0].options][-1] == "8/31（週一）"
        assert "日期頁：**2/2**" in view.prompt()

    def test_selected_days_persist_across_date_pages(self):
        week_values = [week.value for week in time_collection.month_week_options("2026-08")]
        view = DatePresetView(
            "2026-08",
            deadline_ts=1785600000,
            week_values=week_values,
            nature_code=time_collection.DEFAULT_COLLECTION_NATURE,
            selected_day_values=["2026-08-01", "2026-08-30"],
            today=date(2026, 8, 1),
        )

        assert "已選：8/1、8/30" in view.prompt()
        assert "2 天" in view.prompt()

    def test_submitted_days_are_removed_from_next_batch(self):
        week_values = [week.value for week in time_collection.month_week_options("2026-08")]
        view = DatePresetView(
            "2026-08",
            deadline_ts=1785600000,
            week_values=week_values,
            nature_code=time_collection.DEFAULT_COLLECTION_NATURE,
            excluded_day_values={"2026-08-01", "2026-08-02", "2026-08-08"},
            today=date(2026, 8, 1),
        )

        visible_values = [
            option.value
            for page in view.day_pages
            for option in page
        ]
        assert len(visible_values) == 28
        assert "2026-08-01" not in visible_values
        assert "2026-08-02" not in visible_values
        assert "2026-08-08" not in visible_values
        assert "本頁：8/3–8/17" in view.prompt()

    def test_apply_buttons_enable_after_dates_are_selected(self):
        week_values = [week.value for week in time_collection.month_week_options("2026-08")]
        empty_view = DatePresetView(
            "2026-08",
            deadline_ts=1785600000,
            week_values=week_values,
            nature_code=time_collection.DEFAULT_COLLECTION_NATURE,
            today=date(2026, 8, 1),
        )
        selected_view = DatePresetView(
            "2026-08",
            deadline_ts=1785600000,
            week_values=week_values,
            nature_code=time_collection.DEFAULT_COLLECTION_NATURE,
            selected_day_values=["2026-08-01", "2026-08-02"],
            today=date(2026, 8, 1),
        )

        empty_apply_buttons = [
            child for child in empty_view.children
            if getattr(child, "label", None) in {"整天可（08:00–24:00）", "某時間後", "某時間前", "指定時段"}
        ]
        selected_apply_buttons = [
            child for child in selected_view.children
            if getattr(child, "label", None) in {"整天可（08:00–24:00）", "某時間後", "某時間前", "指定時段"}
        ]
        assert all(button.disabled for button in empty_apply_buttons)
        assert all(not button.disabled for button in selected_apply_buttons)

    def test_availability_confirmation_allows_add_or_return(self):
        week_values = [week.value for week in time_collection.month_week_options("2026-08")]
        view = AvailabilityConfirmView(
            "2026-08",
            1785600000,
            time_collection.DEFAULT_COLLECTION_NATURE,
            week_values,
            ["2026-08-12", "2026-08-13"],
            "all_day",
        )

        assert "加入本次回報" in child_labels(view)
        assert "返回修改" in child_labels(view)
        assert "8/12、8/13" in view.prompt()
        assert "08:00–24:00" in view.prompt()

    def test_batch_confirmation_is_the_only_final_submit_step(self):
        groups = [
            AvailabilityDraftGroup(("2026-08-12",), "all_day"),
            AvailabilityDraftGroup(
                ("2026-08-13",),
                "specific",
                start_text="13:00",
                end_text="15:00",
            ),
        ]
        slots = scheduling_artist._draft_slots(groups)
        view = AvailabilityBatchConfirmView(
            "2026-08",
            1785600000,
            time_collection.DEFAULT_COLLECTION_NATURE,
            [week.value for week in time_collection.month_week_options("2026-08")],
            groups,
            original_slots=slots,
            adjusted_windows=slots,
            conflicts=[],
            blocked_slots=[],
        )

        assert "全部確認送出" in child_labels(view)
        assert "返回調整" in child_labels(view)
        assert "2 組、2 個原始時段" in view.prompt()
        assert "按「全部確認送出」後才會一次寫入 Notion" in view.prompt()

    def test_batch_confirmation_explains_past_slots_that_will_be_skipped(self):
        past_slot = (
            datetime(2026, 8, 1, 8, 0, tzinfo=config.TZ),
            datetime(2026, 8, 2, 0, 0, tzinfo=config.TZ),
        )
        future_slot = (
            datetime(2026, 8, 15, 13, 0, tzinfo=config.TZ),
            datetime(2026, 8, 15, 17, 0, tzinfo=config.TZ),
        )
        groups = [AvailabilityDraftGroup(("2026-08-01",), "all_day")]
        view = AvailabilityBatchConfirmView(
            "2026-08",
            1785600000,
            time_collection.DEFAULT_COLLECTION_NATURE,
            [],
            groups,
            original_slots=[past_slot, future_slot],
            adjusted_windows=[future_slot],
            conflicts=[],
            blocked_slots=[],
            past_slots=[past_slot],
        )

        assert "已略過過去時段" in view.prompt()
        assert "8/1（週六）08:00–24:00" in view.prompt()

    def test_conflict_confirmation_shows_adjusted_windows_and_return(self):
        week_values = [week.value for week in time_collection.month_week_options("2026-08")]
        original_slots = [
            (
                datetime(2026, 8, 15, 8, 0, tzinfo=config.TZ),
                datetime(2026, 8, 16, 0, 0, tzinfo=config.TZ),
            )
        ]
        busy = repositories.BusyInterval(
            source_id="task-1",
            source_name="測試課程",
            source_type="Task",
            start=datetime(2026, 8, 15, 13, 30, tzinfo=config.TZ),
            end=datetime(2026, 8, 15, 14, 30, tzinfo=config.TZ),
        )
        context = repositories.ScheduleImpactContext(busy_intervals=[busy])

        adjusted, conflicts, blocked = scheduling_artist._preview_availability_impact(
            original_slots,
            context,
            now=datetime(2026, 8, 1, tzinfo=config.TZ),
        )
        view = AvailabilityConflictConfirmView(
            "2026-08",
            1785600000,
            time_collection.DEFAULT_COLLECTION_NATURE,
            week_values,
            ["2026-08-15"],
            "all_day",
            original_slots=original_slots,
            adjusted_windows=adjusted,
            conflicts=conflicts,
            blocked_slots=blocked,
        )

        prompt = view.prompt()
        assert "8/15（週六）08:00–13:30" in prompt
        assert "8/15（週六）14:30–24:00" in prompt
        assert "你已有以下既定任務" in prompt
        assert "8/15（週六）13:30–14:30｜任務：測試課程" in prompt
        assert blocked == []
        assert "確認調整後送出" in child_labels(view)
        assert "返回修改" in child_labels(view)

    def test_conflict_preview_marks_fully_blocked_slot(self):
        original = (
            datetime(2026, 8, 15, 13, 30, tzinfo=config.TZ),
            datetime(2026, 8, 15, 14, 30, tzinfo=config.TZ),
        )
        context = repositories.ScheduleImpactContext(
            busy_intervals=[
                repositories.BusyInterval(
                    source_id="task-1",
                    source_name="測試課程",
                    source_type="Task",
                    start=original[0],
                    end=original[1],
                )
            ]
        )

        adjusted, conflicts, blocked = scheduling_artist._preview_availability_impact(
            [original],
            context,
            now=datetime(2026, 8, 1, tzinfo=config.TZ),
        )

        assert adjusted == []
        assert len(conflicts) == 1
        assert blocked == [original]

        view = AvailabilityConflictConfirmView(
            "2026-08",
            1785600000,
            time_collection.DEFAULT_COLLECTION_NATURE,
            [week.value for week in time_collection.month_week_options("2026-08")],
            ["2026-08-15"],
            "specific",
            start_text="13:30",
            end_text="14:30",
            original_slots=[original],
            adjusted_windows=adjusted,
            conflicts=conflicts,
            blocked_slots=blocked,
        )
        confirm = next(child for child in view.children if child.label == "確認調整後送出")
        assert confirm.disabled is True
        assert "全部被既定安排占用" in view.prompt()

    def test_schedule_summary_stays_visible_across_date_interactions(self):
        week_values = [week.value for week in time_collection.month_week_options("2026-08")]
        summary = "📌 **你在 2026-08 的既定安排**\n・8/6｜任務：測試任務0806（時間未定）"
        view = DatePresetView(
            "2026-08",
            deadline_ts=1785600000,
            week_values=week_values,
            nature_code=time_collection.DEFAULT_COLLECTION_NATURE,
            schedule_summary=summary,
        )

        assert view.prompt().startswith(summary)
        assert summary in view.page_view(1).prompt()

        view.update_day_values(["2026-08-01"])
        assert summary in view.prompt()


def artist(page_id: str, name: str):
    return repositories.Artist(page_id=page_id, name=name, discord_user_id="123")


class TestPoolNotificationCopy:
    def test_artist_schedule_summary_separates_fixed_and_date_only_items(self):
        context = repositories.ScheduleImpactContext(
            busy_intervals=[
                repositories.BusyInterval(
                    source_id="task-fixed",
                    source_name="例會安排",
                    source_type="Task",
                    start=datetime(2026, 8, 6, 14, 0, tzinfo=config.TZ),
                    end=datetime(2026, 8, 6, 17, 0, tzinfo=config.TZ),
                )
            ],
            warnings=[
                repositories.ScheduleWarning(
                    source_id="task-date-only",
                    source_name="測試任務0806",
                    source_type="Task",
                    start=datetime(2026, 8, 10, 0, 0, tzinfo=config.TZ),
                    end=datetime(2026, 8, 12, 0, 0, tzinfo=config.TZ),
                )
            ],
        )

        summary = scheduling_artist._artist_schedule_summary("2026-08", context)

        assert "8/6 14:00–17:00｜任務：例會安排" in summary
        assert "8/10–8/12｜任務：測試任務0806" in summary
        assert "時間未定" not in summary

    def test_artist_done_notice_includes_progress_and_missing_artists(self):
        artists = [artist("artist-1", "Johnny"), artist("artist-2", "Chenhua")]
        progress = repositories.MonthlyPoolProgress(
            filled_artist_page_ids=["artist-1"],
            missing_artist_page_ids=["artist-2"],
        )

        text = scheduling_artist.pool_artist_done_notice("2026-08", artists[0], artists, progress)

        assert "Johnny 已完成" in text
        assert "1/2" in text
        assert "未填：Chenhua" in text

    def test_all_done_notice_points_admin_to_pool_match(self):
        artists = [artist("artist-1", "Johnny"), artist("artist-2", "Chenhua")]

        text = scheduling_artist.pool_all_done_notice("2026-08", artists)

        assert "已收齊" in text
        assert "/從時間池建立任務" in text
        assert "Johnny、Chenhua" in text


def test_refill_time_command_is_artist_visible():
    command = SchedulingArtistCog.refill_time

    assert command.name == "管理可配合時間"
    assert command.default_permissions is None
    assert inspect.signature(command.callback).parameters["月份"].default is None
    assert not hasattr(SchedulingArtistCog, "my_slots")
