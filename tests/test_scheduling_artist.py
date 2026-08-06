"""Discord artist scheduling UI helper tests."""

from cogs import scheduling_artist
import discord

from cogs.scheduling_artist import DatePresetView, SchedulingArtistCog
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
        assert [len(page) for page in view.day_pages] == [25, 6]
        selects = [child for child in view.children if isinstance(child, discord.ui.Select)]
        assert len(selects) == 1
        assert selects[0].max_values == 25
        assert [option.label for option in selects[0].options][:2] == ["8/1（週六）", "8/2（週日）"]
        labels = child_labels(view)
        assert "下一頁" in labels
        assert "整天可" in labels
        assert "指定時段" in labels
        assert "選全部日期" not in labels
        assert "勾同一批日期" in view.prompt()

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
        assert [option.label for option in selects[0].options] == [
            "8/26（週三）",
            "8/27（週四）",
            "8/28（週五）",
            "8/29（週六）",
            "8/30（週日）",
            "8/31（週一）",
        ]
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

        assert "已勾" in view.prompt()
        assert "2 天" in view.prompt()

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
            if getattr(child, "label", None) in {"整天可", "某時間後", "某時間前", "指定時段"}
        ]
        selected_apply_buttons = [
            child for child in selected_view.children
            if getattr(child, "label", None) in {"整天可", "某時間後", "某時間前", "指定時段"}
        ]
        assert all(button.disabled for button in empty_apply_buttons)
        assert all(not button.disabled for button in selected_apply_buttons)


def artist(page_id: str, name: str):
    return repositories.Artist(page_id=page_id, name=name, discord_user_id="123")


class TestPoolNotificationCopy:
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
        assert "/任務建立與安排" in text
        assert "Johnny、Chenhua" in text


def test_refill_time_command_is_artist_visible():
    command = SchedulingArtistCog.refill_time

    assert command.name == "補填時間"
    assert command.default_permissions is None
