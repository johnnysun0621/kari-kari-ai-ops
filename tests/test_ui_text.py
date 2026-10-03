from datetime import datetime

import config
from cogs import ui_text
from services.repositories import TaskItem


def test_needs_fix_displays_revision_deadline_instead_of_original_submission_deadline():
    task = TaskItem(
        page_id="task-1",
        name="歌唱作業",
        status="需修改",
        task_type="課程與作業",
        open_for_submission=True,
        submission_deadline=datetime(2026, 8, 10, 23, 59, tzinfo=config.TZ),
        submission_deadline_has_time=True,
        revision_deadline=datetime(2026, 8, 18, 12, 0, tzinfo=config.TZ),
    )

    text = ui_text.deadline_text(
        task,
        datetime(2026, 8, 16, 10, 0, tzinfo=config.TZ),
    )

    assert text == "修改期限 8/18 12:00（剩 2 天）"
    assert "8/10" not in text


def test_approval_task_says_submission_date_not_release_date():
    """需審批任務給藝人看的是「交件 9/25」，不是「截止 9/30」——
    看到後者會以為還有五天，然後 9/30 才交，審核來不及。"""
    task = TaskItem(
        page_id="task-1",
        name="新單曲 MV 發布",
        status="進行中",
        task_type="發布上架",
        end=datetime(2026, 9, 30, tzinfo=config.TZ),
        needs_approval=True,
    )

    text = ui_text.deadline_text(task, datetime(2026, 9, 22, 10, 0, tzinfo=config.TZ))

    assert text == "交件 9/25（剩 3 天）"
    assert "9/30" not in text


def test_plain_task_still_says_deadline():
    task = TaskItem(
        page_id="task-1",
        name="舞蹈課",
        status="進行中",
        task_type="課程與作業",
        end=datetime(2026, 9, 17, tzinfo=config.TZ),
    )

    text = ui_text.deadline_text(task, datetime(2026, 9, 15, 10, 0, tzinfo=config.TZ))

    assert text == "截止 9/17（剩 2 天）"


def test_trim_message_cuts_on_line_boundary_instead_of_mid_line():
    text = "\n".join(f"第 {index} 行的內容" for index in range(400))

    trimmed = ui_text.trim_message(text)

    assert len(trimmed) <= ui_text.DISCORD_MESSAGE_LIMIT
    body, _, note = trimmed.rpartition("\n")
    assert note == "...清單過長已截斷"
    assert body.split("\n")[-1] in text.split("\n"), "最後一行被切成破碎文字"


def test_fit_items_keeps_whole_items_and_reports_the_rest():
    items = [f"・項目 {index}" for index in range(50)]

    kept, remaining = ui_text.fit_items(items, budget=60)

    assert kept == items[:len(kept)]
    assert remaining == len(items) - len(kept)
    assert len("\n".join(kept)) <= 60


def test_fit_items_keeps_at_least_one_item_even_when_over_budget():
    kept, remaining = ui_text.fit_items(["非常長的一筆資料" * 20], budget=10)

    assert len(kept) == 1
    assert remaining == 0


def test_paginate_sections_repeats_title_with_continued_marker_across_pages():
    sections = [("**逾期**（40）", [f"・任務 {index}" for index in range(40)])]

    pages = ui_text.paginate_sections(sections, budget=120)

    assert len(pages) > 1
    assert pages[0][0] == "**逾期**（40）"
    for page in pages[1:]:
        assert page[0] == "**逾期**（40）（續）", "跨頁的分區必須重印標題，不能出現孤兒清單"
    listed = [line for page in pages for line in page if line.startswith("・")]
    assert listed == [f"・任務 {index}" for index in range(40)], "分頁後有項目遺失"


def test_paginate_blocks_never_splits_a_block():
    blocks = [[f"**第 {index} 組**", "　內容一", "　內容二"] for index in range(30)]

    pages = ui_text.paginate_blocks(blocks, budget=100)

    assert len(pages) > 1
    for page in pages:
        # 每個 block 的標題後面一定跟著自己的兩行內容，不會被拆到別頁。
        titles = [line for line in page if line.startswith("**")]
        assert len(page) == len(titles) * 3
    assert sum(len(page) for page in pages) == sum(len(block) for block in blocks)


class TestPageWindow:
    """頁數超過 Discord 下拉上限（25）時的取窗邏輯。"""

    def test_small_page_count_lists_every_page(self):
        from cogs.paging import build_page_window

        assert build_page_window(current=0, total=8) == list(range(8))

    def test_large_page_count_keeps_first_last_and_current(self):
        from cogs.paging import build_page_window

        window = build_page_window(current=40, total=100)

        assert len(window) <= 25
        assert window[0] == 0, "第一頁要保留當錨點"
        assert window[-1] == 99, "最後一頁要保留當錨點"
        assert 40 in window, "目前頁一定要在選單裡"

    def test_window_near_start_and_end_stays_in_range(self):
        from cogs.paging import build_page_window

        for current in (0, 1, 98, 99):
            window = build_page_window(current=current, total=100)
            assert current in window
            assert len(window) <= 25
            assert all(0 <= index < 100 for index in window)
