"""Discord artist scheduling UI helper tests."""

from datetime import datetime

from cogs import scheduling_artist
import discord

import config
from cogs.scheduling_artist import AvailabilityConfirmView, DatePresetView, SchedulingArtistCog
from services import repositories, time_collection


def child_labels(view):
    return [getattr(child, "label", None) for child in view.children]


class TestDatePresetView:
    def test_month_dates_are_shown_as_discord_sized_select_pages(self):
        week_values = [week.value for week in time_collection.month_week_options("2026-08")]
        view = DatePresetView(
            "2026-08",
            deadline_ts=1785600000,
            week_values=week_values,
            nature_code=time_collection.DEFAULT_COLLECTION_NATURE,
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


def test_my_slots_requires_explicit_delete_confirmation():
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
    view = scheduling_artist.MySlotsDeleteView([slot], artist)

    labels = child_labels(view)
    assert "刪除選取時段" in labels
    assert "取消" in labels
    assert view.select.callback == view.selection_changed

    def test_last_day_button_page_shows_remaining_month_days(self):
        week_values = [week.value for week in time_collection.month_week_options("2026-08")]
        view = DatePresetView(
            "2026-08",
            deadline_ts=1785600000,
            week_values=week_values,
            nature_code=time_collection.DEFAULT_COLLECTION_NATURE,
            page_index=1,
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
        )
        selected_view = DatePresetView(
            "2026-08",
            deadline_ts=1785600000,
            week_values=week_values,
            nature_code=time_collection.DEFAULT_COLLECTION_NATURE,
            selected_day_values=["2026-08-01", "2026-08-02"],
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

    def test_availability_confirmation_allows_submit_or_return(self):
        week_values = [week.value for week in time_collection.month_week_options("2026-08")]
        view = AvailabilityConfirmView(
            "2026-08",
            1785600000,
            time_collection.DEFAULT_COLLECTION_NATURE,
            week_values,
            ["2026-08-12", "2026-08-13"],
            "all_day",
        )

        assert "確認送出" in child_labels(view)
        assert "返回修改" in child_labels(view)
        assert "8/12、8/13" in view.prompt()
        assert "08:00–24:00" in view.prompt()

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
