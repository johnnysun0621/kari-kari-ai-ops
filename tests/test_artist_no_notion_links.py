"""藝人端零 Notion 入口：一次掃過所有會送到藝人眼前的訊息產生器。

為什麼要有這支：Notion 的逐頁權限管不到頁面上方的導覽列。藝人拿到任何一個頁面
網址，就能往上翻到該頁所屬的資料庫——從一筆任務翻到整個 Tasks（所有藝人的任務），
從一筆會議紀錄翻到整個會議資料庫。連結曾經散在 8 個地方，逐個修掉之後需要一道
守門測試，之後有人新增藝人端訊息又附上連結時會在這裡被擋下來。

控方／老師看得到的輸出不在此列（`/審核提交` 的詳情、營運內部通知、Audit Log），
他們本來就有 Notion 權限。
"""
from datetime import datetime

import config
from cogs.bulletin import format_bulletin
from cogs.submissions import review_dm_text, review_feedback_file_dm_text
from services import course_qa, monthly_board, notion_links, repositories, task_notifications


_NOTION_URL = "https://www.notion.so/3b55806f097b81e8b430e9b9ce902f2a"


def _task(**changes) -> repositories.TaskItem:
    """刻意帶著 Notion url：欄位本身沒錯，錯的是把它寫進藝人看得到的輸出。"""
    values = dict(
        page_id="task-1",
        name="Johnny｜編曲作業",
        status="進行中",
        task_type="通告",
        url=_NOTION_URL,
        # 控方會把參考頁直接貼進任務說明，這條路徑之前漏掉過（2026-10-11 發現）。
        description=f"請在週五前交出第一版編曲，格式 wav。\n參考：{_NOTION_URL}",
        artist_page_ids=["artist-1"],
        discord_visible=True,
        start=datetime(2026, 9, 20, 14, 0, tzinfo=config.TZ),
        end=datetime(2026, 9, 20, 16, 0, tzinfo=config.TZ),
        open_for_submission=True,
    )
    values.update(changes)
    return repositories.TaskItem(**values)


def _submission() -> repositories.SubmissionItem:
    return repositories.SubmissionItem(
        page_id="submission-1",
        name="Johnny｜編曲作業",
        status="需修改",
        artist_page_ids=["artist-1"],
        task_page_ids=["3b55806f-097b-81e8-b430-e9b9ce902f2a"],
        drive_url="https://drive.google.com/folders/abc",
    )


def _meeting() -> repositories.CourseMeetingRecord:
    return repositories.CourseMeetingRecord(
        page_id="meeting-1",
        url="https://www.notion.so/meeting-1",
        title="九月企劃會議",
        date=datetime(2026, 9, 12, tzinfo=config.TZ),
        summary="討論單曲發行時程",
        content="完整會議內容",
        artist_page_ids=("artist-1",),
        task_page_ids=(),
        project_page_ids=(),
    )


def _bulletin() -> repositories.BulletinItem:
    return repositories.BulletinItem(
        page_id="page-1",
        url="https://notion.so/page-1",
        title="九月排練通知",
        category="📢 公告",
        status="未處理",
        date=datetime(2026, 9, 12, tzinfo=config.TZ),
        people=["Johnny"],
        pinned=True,
    )


def _artist_facing_messages() -> dict[str, str]:
    """key 是出處，出問題時直接指出哪一支產生器破功。"""
    task = _task()
    submission = _submission()
    return {
        "任務通知": task_notifications.notification_message(task),
        "任務派發通知": task_notifications.assignment_notification_message(task),
        "未確認提醒": task_notifications.acknowledgement_reminder_message([task]),
        "截止前提醒": task_notifications.reminder_message(task, "3 天前", "截止 09/20"),
        "同仁知會": task_notifications.staff_message(task_notifications.notification_message(task)),
        "公告區公開公告": task_notifications.public_announcement_message(task),
        "審核結果 DM": review_dm_text(
            submission,
            task_name="編曲作業",
            result="需修改",
            feedback="請調整音量",
            reviewer="A 老師",
        ),
        "批改檔 DM": review_feedback_file_dm_text(
            submission,
            result="通過",
            feedback="做得很好",
            reviewer="A 老師",
            file_links=["https://drive.google.com/file/xyz"],
        ),
        "公告區布告欄": format_bulletin(_bulletin()),
        "公告區月度排程": monthly_board.render(
            task.start.date(),
            monthly_board.board_lines([_task(name=f"排練 {_NOTION_URL}")], {"artist-1": "Johnny"}),
            ("通告",), task.start,
        ),
        "小律月份總覽": course_qa.build_month_overview_answer([_meeting()], "2026-09"),
        "小律降級回答": course_qa.build_course_notes_fallback([_meeting()]),
    }


def test_no_artist_facing_message_contains_a_notion_link():
    offenders = {
        source: text
        for source, text in _artist_facing_messages().items()
        if notion_links.contains_notion_link(text)
    }

    assert not offenders, f"這些藝人端訊息帶著 Notion 入口：{sorted(offenders)}"


def test_llm_context_carries_no_notion_link():
    """模型會照抄 context 裡的網址，所以連餵進去的資料都不能帶。"""
    context = course_qa.build_context([_meeting()])

    assert not notion_links.contains_notion_link(context)
    # 但來源仍要可辨識，否則模型無法標明答案出自哪一場會議。
    assert "九月企劃會議" in context


def test_artist_facing_messages_still_carry_the_content_artists_need():
    """拿掉連結不能連內容一起拿掉——藝人本來就是靠這些訊息做事的。"""
    messages = _artist_facing_messages()

    assert "請在週五前交出第一版編曲" in messages["任務通知"]
    assert "Johnny｜編曲作業" in messages["任務通知"]
    assert "請調整音量" in messages["審核結果 DM"]
    assert "九月排練通知" in messages["公告區布告欄"]


def test_non_notion_links_survive_for_artists():
    """Drive 批改檔、參考影片等連結是藝人該拿到的，不能被一起砍掉。"""
    text = review_feedback_file_dm_text(
        _submission(),
        result="通過",
        feedback="參考這支 https://youtu.be/abc123",
        reviewer="A 老師",
        file_links=["https://drive.google.com/file/xyz"],
    )

    assert "https://drive.google.com/file/xyz" in text
    assert "https://youtu.be/abc123" in text


def test_staff_message_has_no_button_prompts():
    """同仁沒有按鈕：派發與提醒訊息裡「請先按收到任務／收到通知」要拿掉，不然會跟開頭的「不需要按收到」矛盾。"""
    task = _task()
    for artist_text in (
        task_notifications.notification_message(task),
        task_notifications.assignment_notification_message(task),
        task_notifications.reminder_message(task, "3 天前", "10/25"),
    ):
        staff_text = task_notifications.staff_message(artist_text)
        assert "不需要按收到" in staff_text
        assert "請先按" not in staff_text
        assert "收到通知" not in staff_text
        assert task.name in staff_text
