import asyncio
import re
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import config
from cogs import scheduling_admin
from services import repositories


def test_overlap_query_artist_select_view_exposes_select_and_cancel():
    operator = SimpleNamespace(id=123)
    artist = SimpleNamespace(page_id="artist-1", name="Johnny", status="已簽約")

    view = scheduling_admin.OverlapQueryArtistSelectView(
        [artist], "2026-08", 60, operator,
    )

    assert any(getattr(child, "label", "") == "取消" for child in view.children)
    assert view.select.callback == view.on_select
    assert view.select.min_values == 1


def test_overlap_query_result_view_exposes_create_task_and_close():
    operator = SimpleNamespace(id=123)
    artist = SimpleNamespace(page_id="artist-1", name="Johnny", status="已簽約")
    start = datetime(2026, 8, 13, 13, 30, tzinfo=config.TZ)
    end = datetime(2026, 8, 13, 14, 30, tzinfo=config.TZ)

    view = scheduling_admin.OverlapQueryResultView(
        "2026-08", 60, [artist], [(start, end)], operator, [artist], "摘要文字",
    )
    labels = [getattr(child, "label", "") for child in view.children]

    assert "直接建立任務" in labels
    assert "關閉" in labels


def test_overlap_query_on_select_never_writes_to_notion(monkeypatch):
    """/時間交集查詢 存在的意義就是唯讀；不該呼叫會整理/寫入時間池的函式。"""
    operator = SimpleNamespace(id=123)
    artist = SimpleNamespace(page_id="artist-1", name="Johnny", status="已簽約")

    view = scheduling_admin.OverlapQueryArtistSelectView([artist], "2026-08", 60, operator)
    view.select._values = ["artist-1"]

    compaction_called = False

    async def fake_compaction(slots):
        nonlocal compaction_called
        compaction_called = True
        return SimpleNamespace(visible=slots, redundant=[], updates=[])

    async def fake_slots(artist_page_ids, future_only=True):
        return []

    async def fake_context(artist_page_id):
        return SimpleNamespace(busy_intervals=[], warnings=[])

    match_result = SimpleNamespace(windows=[], missing_artist_page_ids=[])

    monkeypatch.setattr(scheduling_admin.repositories, "apply_availability_slot_compaction", fake_compaction)
    monkeypatch.setattr(scheduling_admin.repositories, "get_monthly_pool_slots_for_artists", fake_slots)
    monkeypatch.setattr(scheduling_admin.repositories, "get_schedule_impact_context_for_artist", fake_context)
    monkeypatch.setattr(scheduling_admin.repositories, "match_monthly_pool_windows", lambda **kwargs: match_result)

    interaction = SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    asyncio.run(view.on_select(interaction))

    assert compaction_called is False
    interaction.followup.send.assert_awaited_once()


def test_duration_candidates_use_requested_duration_and_30_minute_steps():
    start = datetime(2026, 8, 13, 13, 30, tzinfo=config.TZ)
    end = datetime(2026, 8, 13, 18, 0, tzinfo=config.TZ)

    candidates = scheduling_admin._duration_candidates([(start, end)], 60)

    assert candidates[0] == (start, datetime(2026, 8, 13, 14, 30, tzinfo=config.TZ))
    assert candidates[-1] == (
        datetime(2026, 8, 13, 17, 0, tzinfo=config.TZ),
        datetime(2026, 8, 13, 18, 0, tzinfo=config.TZ),
    )
    assert len(candidates) == 8


def test_duration_candidates_do_not_truncate_candidates_by_discord_page_size():
    start = datetime(2026, 8, 1, 8, 0, tzinfo=config.TZ)
    end = datetime(2026, 8, 5, 18, 0, tzinfo=config.TZ)

    candidates = scheduling_admin._duration_candidates([(start, end)], 60)

    assert len(candidates) > scheduling_admin.CANDIDATE_PAGE_SIZE
    assert candidates[-1][1] == datetime(2026, 8, 5, 18, 0, tzinfo=config.TZ)


def test_candidate_result_is_grouped_by_date():
    candidates = [
        (datetime(2026, 8, 13, 13, 30, tzinfo=config.TZ), datetime(2026, 8, 13, 14, 30, tzinfo=config.TZ)),
        (datetime(2026, 8, 13, 14, 0, tzinfo=config.TZ), datetime(2026, 8, 13, 15, 0, tzinfo=config.TZ)),
        (datetime(2026, 8, 14, 13, 30, tzinfo=config.TZ), datetime(2026, 8, 14, 14, 30, tzinfo=config.TZ)),
    ]

    lines = scheduling_admin._format_candidate_groups(candidates)

    assert lines == [
        "**8/13（週四）**",
        "　13:30–14:30　14:00–15:00",
        "**8/14（週五）**",
        "　13:30–14:30",
    ]


def test_monthly_pool_result_text_truncates_on_whole_day_boundaries_when_too_long():
    """一個排滿的月份會產生很多候選天數；訊息超過 Discord 上限時，不該在某一天的
    標題或時段行中間硬切（曾經發生過 `9/22（` 這種被切一半的破碎輸出）。"""
    artist = SimpleNamespace(page_id="artist-1", name="Johnny")
    windows = [
        (
            datetime(2026, 9, day, 8, 0, tzinfo=config.TZ),
            datetime(2026, 9, day, 22, 0, tzinfo=config.TZ),
        )
        for day in range(1, 31)
    ]
    result = SimpleNamespace(windows=windows, missing_artist_page_ids=[])

    text = scheduling_admin._monthly_pool_result_text("2026-09", 60, [artist], result)

    assert len(text) <= scheduling_admin.ui_text.DISCORD_MESSAGE_LIMIT
    date_header_re = re.compile(r"^\d{1,2}/\d{1,2}")
    for line in text.split("\n"):
        stripped = line.strip().strip("*")
        if date_header_re.match(stripped):
            assert re.fullmatch(r"\d{1,2}/\d{1,2}（週[一二三四五六日]）", stripped), (
                f"日期標題被截斷成破碎文字：{line!r}"
            )
    assert "僅顯示前" in text
    assert "這裡只列候選" in text


def _busy_month_result(days: int = 30):
    windows = [
        (
            datetime(2026, 9, day, 8, 0, tzinfo=config.TZ),
            datetime(2026, 9, day, 22, 0, tzinfo=config.TZ),
        )
        for day in range(1, days + 1)
    ]
    return SimpleNamespace(windows=windows, missing_artist_page_ids=[])


def test_overlap_query_pages_cover_every_candidate_day():
    """唯讀查詢要讓控方看到「全部」時段：分頁後每一天都必須出現在某一頁，一天都不能少。"""
    artist = SimpleNamespace(page_id="artist-1", name="Johnny")
    result = _busy_month_result()

    pages = scheduling_admin._overlap_query_pages("2026-09", 60, [artist], result)

    assert len(pages) > 1, "排滿的月份應該要分成多頁，而不是塞進一頁"
    joined = "\n".join(pages)
    for day in range(1, 31):
        assert f"9/{day}（" in joined, f"9/{day} 的候選時段在分頁後不見了"
    for text in pages:
        assert len(text) <= scheduling_admin.ui_text.DISCORD_MESSAGE_LIMIT
        assert "...清單過長已截斷" not in text, "分頁後不應該再有任何一頁被截斷"


def test_overlap_query_result_view_paginates_through_all_pages():
    operator = SimpleNamespace(id=123)
    artist = SimpleNamespace(page_id="artist-1", name="Johnny")
    result = _busy_month_result()
    pages = scheduling_admin._overlap_query_pages("2026-09", 60, [artist], result)

    view = scheduling_admin.OverlapQueryResultView(
        "2026-09", 60, [artist], result.windows, operator, [artist], pages[0], pages=pages,
    )

    assert view.page_count == len(pages)
    assert view.previous_page.disabled is True, "第一頁不該能往前翻"
    assert view.next_page.disabled is False

    seen = [view.pages[view.page]]
    while view.page < view.page_count - 1:
        view.page += 1
        view._sync_page_buttons()
        seen.append(view.pages[view.page])

    assert seen == pages
    assert view.next_page.disabled is True, "最後一頁不該能再往後翻"
    assert view.previous_page.disabled is False


def test_cleanup_preview_stays_within_discord_limit_with_long_slot_names():
    """整理預覽是破壞性操作前的確認畫面；時段名稱很長時也不該被切成破碎文字，
    結尾的確認說明更不能被吃掉。"""
    slots = [
        repositories.AvailabilitySlot(
            page_id=f"slot-{index}",
            name=f"{index:02d} 號藝人回填的月度可配合時段（含備註與補充說明）",
            artist_page_ids=["artist-1"],
            start=datetime(2026, 9, 1, 9, 0, tzinfo=config.TZ),
            end=datetime(2026, 9, 1, 12, 0, tzinfo=config.TZ),
            note="收集月份：2026-09",
        )
        for index in range(40)
    ]
    compaction = repositories.AvailabilitySlotCompaction(
        visible=[],
        redundant=slots[20:],
        updates=[
            repositories.AvailabilitySlotWindowUpdate(
                page_id=slot.page_id,
                start=datetime(2026, 9, 1, 9, 0, tzinfo=config.TZ),
                end=datetime(2026, 9, 1, 18, 0, tzinfo=config.TZ),
            )
            for slot in slots[:20]
        ],
    )

    text = scheduling_admin._cleanup_preview_text(slots, compaction)

    assert len(text) <= scheduling_admin.ui_text.DISCORD_MESSAGE_LIMIT
    assert "...清單過長已截斷" not in text
    assert "確認後才會更新 / 封存 Notion 資料。" in text, "結尾的確認說明不能被截斷掉"
    assert "另有" in text, "沒列出的筆數要誠實說明"


def test_overlap_query_result_view_hides_pagination_for_single_page():
    operator = SimpleNamespace(id=123)
    artist = SimpleNamespace(page_id="artist-1", name="Johnny")
    start = datetime(2026, 8, 13, 13, 30, tzinfo=config.TZ)
    end = datetime(2026, 8, 13, 14, 30, tzinfo=config.TZ)

    view = scheduling_admin.OverlapQueryResultView(
        "2026-08", 60, [artist], [(start, end)], operator, [artist], "摘要文字",
    )

    assert view.page_count == 1
    assert view.previous_page.disabled is True
    assert view.next_page.disabled is True


def test_candidate_flow_views_expose_back_navigation():
    operator = SimpleNamespace(id=123)
    artist = SimpleNamespace(page_id="artist-1", name="Johnny", status="已簽約")
    start = datetime(2026, 8, 13, 13, 30, tzinfo=config.TZ)
    end = datetime(2026, 8, 13, 14, 30, tzinfo=config.TZ)

    candidate_view = scheduling_admin.MonthlyPoolCandidateSelectView(
        "2026-08", 60, [artist], [(start, end)], operator,
    )
    assert any(getattr(child, "label", "") == "返回藝人選擇" for child in candidate_view.children)

    task_view = scheduling_admin.TaskTypeView(
        "2026-08", 60, [artist], [(start, end)], operator, ["通告", "課程與作業"],
    )
    assert any(getattr(child, "label", "") == "返回候選時段" for child in task_view.children)

    artist_view = scheduling_admin.MonthlyPoolArtistSelectView(
        [artist], "2026-08", 60, operator,
    )
    assert any(getattr(child, "label", "") == "取消" for child in artist_view.children)

    course_view = scheduling_admin.CourseTypeView(
        "2026-08", 60, [artist], [(start, end)], operator,
        ["課程與作業"], ["歌唱課", "編曲課"], available_artists=[artist],
    )
    assert any(getattr(child, "label", "") == "返回任務類型" for child in course_view.children)


def test_candidate_pagination_keeps_back_navigation():
    operator = SimpleNamespace(id=123)
    artist = SimpleNamespace(page_id="artist-1", name="Johnny", status="已簽約")
    window = (
        datetime(2026, 8, 1, 8, 0, tzinfo=config.TZ),
        datetime(2026, 8, 1, 9, 0, tzinfo=config.TZ),
    )
    windows = [window] * (scheduling_admin.CANDIDATE_PAGE_SIZE + 1)

    candidate_view = scheduling_admin.MonthlyPoolCandidateSelectView(
        "2026-08", 60, [artist], windows, operator,
    )
    candidate_view.clear_items()
    candidate_view._build_components()
    candidate_view._add_navigation_items()
    assert any(getattr(child, "label", "") == "返回藝人選擇" for child in candidate_view.children)

    direct_view = scheduling_admin.DirectArrangementCandidateView(
        "通告", "通告", "2026-08", 60, [artist], windows, operator,
        ["通告"], available_artists=[artist],
    )
    direct_view.clear_items()
    direct_view._build_components()
    direct_view._add_navigation_items()
    assert any(getattr(child, "label", "") == "返回任務類型" for child in direct_view.children)


def test_course_teacher_view_accepts_back_navigation_context():
    operator = SimpleNamespace(id=123)
    artist = SimpleNamespace(page_id="artist-1", name="Johnny", status="已簽約")
    project = SimpleNamespace(page_id="project-1", name="2026 年 8 月通識課")

    view = scheduling_admin.TeacherSelectView(
        "2026-08", 60, [artist], [], operator, "歌唱課",
        [("teacher-1", "老師")], "project-1", [project], ["課程與作業"], [artist],
    )

    assert any(getattr(child, "label", "") == "返回 Project" for child in view.children)
