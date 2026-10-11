from datetime import datetime

import config
from cogs import ui_text
from cogs.tasks_artist import DispatchOutcome, MyTasksView, TaskResendView, task_list_pages
from services import repositories, task_notifications


def make_task(
    *,
    page_id: str = "task-1",
    name: str = "歌唱課作業",
    end: datetime | None = None,
    discord_notified_at: datetime | None = None,
    discord_acknowledged_at: datetime | None = None,
) -> repositories.TaskItem:
    return repositories.TaskItem(
        page_id=page_id,
        name=name,
        status="尚未開始",
        task_type="課程時間&作業繳交",
        end=end,
        end_has_time=end is not None,
        artist_page_ids=["artist-1"],
        discord_visible=True,
        discord_notified_at=discord_notified_at,
        discord_acknowledged_at=discord_acknowledged_at,
    )


def test_my_tasks_view_only_shows_submission_entry_when_tasks_are_submittable():
    with_submission = MyTasksView(object(), submittable_count=2)
    without_submission = MyTasksView(object(), submittable_count=0)

    def submit_labels(view):
        # 翻頁按鈕是共用分頁介面的一部分，這個測試只看提交入口。
        return [
            item.label for item in view.children
            if item.label not in ("上一頁", "下一頁")
        ]

    assert submit_labels(with_submission) == ["提交任務（2）"]
    assert submit_labels(without_submission) == []


def test_my_tasks_view_disables_paging_buttons_for_single_page():
    view = MyTasksView(object(), submittable_count=0, pages=["只有一頁"])

    assert view.page_count == 1
    assert view.previous_page.disabled is True
    assert view.next_page.disabled is True


def test_task_list_pages_keep_every_task_when_the_list_is_long():
    """任務多到一頁放不下時要分頁，不能讓後面的任務直接消失。"""
    now = datetime(2026, 8, 1, 10, 0, tzinfo=config.TZ)
    tasks = [
        make_task(page_id=f"task-{index}", name=f"任務名稱編號 {index:03d}")
        for index in range(80)
    ]
    buckets = {"open": tasks}

    pages, labels = task_list_pages("📋 未結案任務總覽（80 筆）：", buckets, now)

    assert len(labels) == len(pages)

    assert len(pages) > 1, "80 筆任務應該要分頁"
    joined = "\n".join(pages)
    for index in range(80):
        assert f"任務名稱編號 {index:03d}" in joined, f"第 {index} 筆任務在分頁後不見了"
    for text in pages:
        assert len(text) <= ui_text.DISCORD_MESSAGE_LIMIT
        assert "...清單過長已截斷" not in text
        assert "第 " in text and " 頁）" in text, "分頁後每頁都要標示頁碼"


def test_notification_message_explains_task_and_submission_method():
    task = make_task(end=datetime(2026, 8, 5, 18, 0, tzinfo=config.TZ))
    task.description = "請完成本週歌唱練習並提交錄音檔。"

    message = task_notifications.notification_message(task)

    assert "歌唱課作業" in message
    assert "08/05 18:00" in message
    assert "檔案" in message
    assert "請完成本週歌唱練習" in message
    assert "收到任務" in message


def test_course_notification_hides_internal_candidates_and_shows_formal_time():
    task = make_task(name="跳舞課(測試)")
    task.course_category = "舞蹈課"
    task.start = datetime(2026, 8, 14, 14, 30, tzinfo=config.TZ)
    task.end = datetime(2026, 8, 14, 15, 30, tzinfo=config.TZ)
    task.start_has_time = True
    task.end_has_time = True
    task.description = (
        "安排來源：/從時間池建立任務\n"
        "課程類別：舞蹈課\n"
        "負責老師：子喬\n"
        "候選時段（由營運最後確認）：\n"
        "2026-08-14 14:30~15:30\n"
        "此 Task 同時管理課程場次與課後作業；正式時間請由營運直接填寫開始日。"
    )

    message = task_notifications.notification_message(task)

    assert "時間：08/14 14:30–15:30" in message
    assert "截止：" not in message
    assert "候選時段" not in message
    assert "安排來源" not in message
    assert "負責老師：子喬" in message


def test_notification_always_shows_task_content_and_drive_upload_guide():
    """提交方式已停用（M4-03）：通知一律帶任務內容，並指向公司 Drive 上傳。"""
    task = make_task()
    task.description = "請完成舞蹈練習，並於課後提交錄影。"

    message = task_notifications.notification_message(task)

    assert "提交方式" not in message
    assert "**任務內容**" in message
    assert "請完成舞蹈練習，並於課後提交錄影。" in message
    assert "公司 Drive 資料夾" in message


def test_assignment_notification_requires_open_submission_and_has_assignment_copy():
    task = make_task()
    task.description = "請上傳課後練習錄影。"
    task.end = datetime(2026, 8, 14, 15, 30, tzinfo=config.TZ)
    task.end_has_time = True
    task.submission_deadline = datetime(2026, 8, 20, 23, 59, tzinfo=config.TZ)
    task.submission_deadline_has_time = True

    assert not task_notifications.is_assignment_dispatchable(task)
    task.open_for_submission = True
    assert task_notifications.is_assignment_dispatchable(task)
    message = task_notifications.assignment_notification_message(task)
    assert "課後作業通知" in message
    assert "請上傳課後練習錄影。" in message
    assert "公司 Drive 資料夾" in message
    assert "提交方式" not in message
    assert "截止：08/20 23:59" in message
    assert "08/14 15:30" not in message


def test_assignment_notification_state_survives_round_trip(tmp_path):
    path = tmp_path / "assignment_notifications.json"
    state = task_notifications.load_assignment_state(path)
    assert not task_notifications.was_assignment_notified("task-1", state)

    state = task_notifications.mark_assignment_notified("task-1", state)
    task_notifications.save_assignment_state(state, path)

    assert task_notifications.was_assignment_notified(
        "task-1", task_notifications.load_assignment_state(path)
    )


def test_public_announcement_uses_configured_task_types():
    """設定值必須是 Notion `任務類型` 既有的選項，這裡固定用真實選項當樣本。"""
    course = make_task()                       # 課程與作業，不公開
    public = make_task(name="八月通告")
    public.task_type = "通告"

    assert not task_notifications.is_public_announcement_task(course, ("通告", "演出"))
    assert task_notifications.is_public_announcement_task(public, ("通告", "演出"))


def test_public_announcement_ignores_types_absent_from_notion():
    """設定填了 Notion 沒有的類型（例如曾經的「拍攝」）不會報錯，但也永遠不命中。"""
    task = make_task(name="八月通告")
    task.task_type = "通告"

    assert not task_notifications.is_public_announcement_task(task, ("拍攝",))


def test_acknowledgement_reminder_lists_each_unconfirmed_task():
    tasks = [make_task(page_id="task-a", name="歌唱課作業"), make_task(page_id="task-b", name="舞蹈課作業")]

    message = task_notifications.acknowledgement_reminder_message(tasks)

    assert "歌唱課作業" in message
    assert "舞蹈課作業" in message
    assert "重新送出任務通知" in message


def test_resend_list_includes_already_acknowledged_tasks():
    """控方改了時間或內容後要能重發，即使藝人已按過「收到任務」。

    以前排除已確認的任務，控方只能手動清空 Notion 的兩個時間欄位才叫得出來。
    """
    pending = make_task(page_id="pending")
    received = make_task(
        page_id="received",
        discord_acknowledged_at=datetime(2026, 8, 2, 12, 0, tzinfo=config.TZ),
    )

    assert task_notifications.is_resendable_task(pending)
    assert task_notifications.is_resendable_task(received)


def test_resend_list_still_excludes_hidden_and_closed_tasks():
    hidden = make_task(page_id="hidden")
    hidden.discord_visible = False
    done = make_task(page_id="done")
    done.status = "已完成"

    assert not task_notifications.is_resendable_task(hidden)
    assert not task_notifications.is_resendable_task(done)


def test_resend_list_sorts_unconfirmed_first():
    """Discord 下拉只放得下 25 個，最常要重送的（未確認）必須排前面。"""
    confirmed = make_task(
        page_id="confirmed",
        name="AAA 已確認",
        discord_acknowledged_at=datetime(2026, 8, 2, 12, 0, tzinfo=config.TZ),
    )
    pending = make_task(page_id="pending", name="ZZZ 未確認")

    ordered = task_notifications.sort_resendable_tasks([confirmed, pending])

    assert [t.page_id for t in ordered] == ["pending", "confirmed"]


def test_acknowledgement_scan_runs_on_fixed_twelve_hour_interval():
    now = datetime(2026, 8, 3, 0, 0, tzinfo=config.TZ)

    assert not task_notifications.should_run_acknowledgement_scan(
        datetime(2026, 8, 2, 12, 1, tzinfo=config.TZ), now
    )
    assert task_notifications.should_run_acknowledgement_scan(
        datetime(2026, 8, 2, 12, 0, tzinfo=config.TZ), now
    )


def test_first_state_bootstrap_does_not_notify_existing_tasks():
    tasks = [make_task(page_id="old-1"), make_task(page_id="old-2")]

    unseen, next_state = task_notifications.find_unseen_tasks(tasks, {})

    assert unseen == []
    assert next_state["initialized"] is True
    assert next_state["known_task_ids"] == ["old-1", "old-2"]


def test_new_task_is_notified_once_after_bootstrap():
    state = {"initialized": True, "known_task_ids": ["old-1"]}
    tasks = [make_task(page_id="old-1"), make_task(page_id="new-1")]

    unseen, next_state = task_notifications.find_unseen_tasks(tasks, state)

    assert [task.page_id for task in unseen] == ["new-1"]
    assert next_state["known_task_ids"] == ["old-1", "new-1"]


def test_failed_task_can_be_removed_for_retry():
    state = {"initialized": True, "known_task_ids": ["old-1", "new-1"]}

    next_state = task_notifications.forget_task("new-1", state)

    assert next_state["known_task_ids"] == ["old-1"]


def test_existing_unnotified_task_is_dispatchable_after_bootstrap():
    task = make_task(page_id="existing-1")

    pending, _ = task_notifications.find_dispatch_tasks(
        [task], {"initialized": True, "known_task_ids": []}
    )

    assert [item.page_id for item in pending] == ["existing-1"]


def test_notified_task_is_not_dispatchable_again():
    task = make_task(
        page_id="existing-1",
        discord_notified_at=datetime(2026, 8, 2, 12, 0, tzinfo=config.TZ),
    )

    pending, _ = task_notifications.find_dispatch_tasks(
        [task], {"initialized": True, "known_task_ids": []}
    )

    assert pending == []


def test_hidden_notified_task_is_selected_for_notification_reset():
    task = make_task(
        page_id="hidden-1",
        discord_notified_at=datetime(2026, 8, 2, 12, 0, tzinfo=config.TZ),
        discord_acknowledged_at=datetime(2026, 8, 2, 12, 5, tzinfo=config.TZ),
    )
    task.discord_visible = False

    pending = task_notifications.find_hidden_tasks_needing_notification_reset([task])

    assert [item.page_id for item in pending] == ["hidden-1"]


def test_hidden_task_without_notification_history_does_not_need_reset():
    task = make_task(page_id="hidden-clean")
    task.discord_visible = False

    assert task_notifications.find_hidden_tasks_needing_notification_reset([task]) == []


def test_acknowledgement_props_store_the_received_time():
    received_at = datetime(2026, 8, 2, 12, 30, tzinfo=config.TZ)

    props = task_notifications.acknowledgement_props(received_at)

    assert props["Discord 收到時間"]["date"]["start"] == received_at.isoformat()


def test_task_resend_requires_explicit_confirmation():
    view = TaskResendView([make_task()])
    labels = [getattr(item, "label", "") for item in view.children]

    assert "確認重送" in labels
    assert "取消" in labels
    assert view.select.callback == view.selection_changed


def test_dispatch_outcome_is_complete_only_when_all_notifyable_artists_sent():
    artist = repositories.Artist(page_id="artist-1", name="Johnny", discord_user_id="123")

    assert DispatchOutcome(notifyable_artists=[artist], sent=1).is_complete is True
    assert DispatchOutcome(notifyable_artists=[artist], sent=0).is_complete is False
    assert DispatchOutcome(notifyable_artists=[], sent=0).is_complete is False


def test_format_reminder_interval_shows_seconds_under_one_minute():
    assert task_notifications.format_reminder_interval(0.5) == "30 秒"


def test_format_reminder_interval_shows_minutes_without_trailing_zero():
    assert task_notifications.format_reminder_interval(30) == "30 分鐘"
    assert task_notifications.format_reminder_interval(10.5) == "10.5 分鐘"


def test_keyword_filter_narrows_resend_list():
    """任務一多，25 個下拉選項不夠用；關鍵字要能縮小範圍。"""
    tasks = [
        make_task(page_id="a", name="編曲課-2609"),
        make_task(page_id="b", name="歌唱課-2609"),
        make_task(page_id="c", name="月夜＿內容確認"),
    ]

    assert [t.page_id for t in task_notifications.filter_tasks_by_keyword(tasks, "編曲")] == ["a"]
    assert [t.page_id for t in task_notifications.filter_tasks_by_keyword(tasks, "2609")] == ["a", "b"]
    assert len(task_notifications.filter_tasks_by_keyword(tasks, "不存在")) == 0


def test_keyword_filter_is_case_insensitive_and_optional():
    tasks = [make_task(page_id="a", name="Demo 錄音")]

    assert task_notifications.filter_tasks_by_keyword(tasks, "demo")
    assert task_notifications.filter_tasks_by_keyword(tasks, "DEMO")
    # 留空或只有空白＝不過濾
    assert task_notifications.filter_tasks_by_keyword(tasks, None) == tasks
    assert task_notifications.filter_tasks_by_keyword(tasks, "   ") == tasks


# ---------- 截止前提醒（T-5／當天）與藝人確認 ----------

SEPT_25 = datetime(2026, 9, 25, 9, 12, tzinfo=config.TZ)


def test_reminder_stage_codes_round_trip_and_overdue_has_none():
    """custom_id 只能放短代碼；逾期沒有確認按鈕。"""
    for stage in ("5 天前", "3 天前", "1 天前", "當天"):
        code = task_notifications.reminder_stage_code(stage)
        assert code is not None
        assert task_notifications.reminder_stage_from_code(code) == stage
    assert task_notifications.reminder_stage_code(repositories.OVERDUE_STAGE) is None
    assert task_notifications.reminder_stage_from_code("zz") is None


def test_reminder_message_carries_content_because_artist_has_no_notion_link():
    task = make_task(end=datetime(2026, 9, 25, tzinfo=config.TZ))
    task.description = "安排來源：/從時間池建立任務\n請交第一版編曲，wav 格式。"

    text = task_notifications.reminder_message(task, "當天", "交件 9/25（今天）")

    assert "任務提醒（當天）" in text
    assert "歌唱課作業" in text
    assert "交件 9/25（今天）" in text
    assert "提交方式" not in text
    assert "請交第一版編曲" in text
    assert "安排來源" not in text          # 控方內部行仍要濾掉
    assert "收到通知" in text
    assert "notion.so" not in text


def test_reminder_message_without_description_still_reads_cleanly():
    task = make_task()

    text = task_notifications.reminder_message(task, "3 天前", "截止 9/28（剩 3 天）")

    assert "任務內容" not in text
    assert "收到通知" in text


def test_reminder_ack_and_sent_lines_share_one_format():
    assert task_notifications.format_reminder_ack_line("當天", SEPT_25, "Johnny") == "當天｜09/25 09:12｜Johnny"
    assert task_notifications.format_reminder_sent_line("當天", SEPT_25, 3) == "當天｜09/25 09:12｜已發送給 3 人"


def test_acknowledged_artists_ignores_sent_marker_and_other_stages():
    text = (
        "3 天前｜09/22 09:00｜已發送給 3 人\n"
        "3 天前｜09/22 14:30｜Johnny\n"
        "3 天前｜09/22 19:02｜小明\n"
        "當天｜09/25 09:00｜已發送給 3 人\n"
        "當天｜09/25 09:12｜Johnny\n"
        "亂七八糟的一行"
    )

    assert task_notifications.acknowledged_artists(text, "3 天前") == {"Johnny", "小明"}
    assert task_notifications.acknowledged_artists(text, "當天") == {"Johnny"}
    assert task_notifications.acknowledged_artists(text, "1 天前") == set()


def test_reminder_ack_summary_reports_latest_stage_against_all_artists():
    """/任務總覽 那一行：對照關聯藝人算 x/y，控方一眼知道要戳誰。"""
    task = make_task()
    task.reminder_ack = (
        "3 天前｜09/22 09:00｜已發送給 3 人\n"
        "3 天前｜09/22 14:30｜Johnny\n"
        "3 天前｜09/22 19:02｜小明\n"
        "當天｜09/25 09:00｜已發送給 3 人\n"
        "當天｜09/25 09:12｜Johnny"
    )

    summary = task_notifications.reminder_ack_summary(task, ["Johnny", "小明", "小 A"])

    assert summary == "提醒確認（當天）：1/3 ✓（Johnny）"


def test_reminder_ack_summary_is_empty_before_any_reminder():
    assert task_notifications.reminder_ack_summary(make_task(), ["Johnny"]) == ""


def test_reminder_ack_summary_shows_zero_when_sent_but_nobody_confirmed():
    """「發了沒人按」要跟「還沒發」分得出來，這就是為什麼送出時也記一行。"""
    task = make_task()
    task.reminder_ack = "當天｜09/25 09:00｜已發送給 2 人"

    assert task_notifications.reminder_ack_summary(task, ["Johnny", "小明"]) == "提醒確認（當天）：0/2 ✓"


def test_admin_overview_shows_ack_line_but_artist_view_does_not():
    from cogs.tasks_artist import task_bucket_sections

    task = make_task(end=datetime(2026, 9, 25, tzinfo=config.TZ))
    task.reminder_ack = "當天｜09/25 09:00｜已發送給 1 人\n當天｜09/25 09:12｜Johnny"
    buckets = {"due_soon": [task]}
    now = datetime(2026, 9, 25, 10, 0, tzinfo=config.TZ)

    admin = task_bucket_sections(buckets, now, name_of_artist={"artist-1": "Johnny"})
    artist = task_bucket_sections(buckets, now, name_of_artist=None)

    admin_lines = "\n".join(line for _, lines in admin for line in lines)
    artist_lines = "\n".join(line for _, lines in artist for line in lines)
    assert "提醒確認（當天）：1/1 ✓（Johnny）" in admin_lines
    assert "提醒確認" not in artist_lines


class TestReminderAckButton:
    """按鈕本身的接線：驗身分、寫確認、回覆。Notion 呼叫全部 monkeypatch。"""

    @staticmethod
    def _interaction(user_id: int = 123):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        return SimpleNamespace(
            user=SimpleNamespace(id=user_id),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    def _wire(self, monkeypatch, *, task=None, artist=None):
        import asyncio
        from cogs import tasks_artist

        appended = []

        async def fake_get_task(task_id):
            return task

        async def fake_find_artist(discord_id):
            return artist

        async def fake_append(page_id, line):
            appended.append((page_id, line))
            return True

        async def fake_audit(**kwargs):
            pass

        monkeypatch.setattr(tasks_artist.repositories, "get_task", fake_get_task)
        monkeypatch.setattr(tasks_artist.repositories, "find_artist_by_discord_id", fake_find_artist)
        monkeypatch.setattr(tasks_artist.repositories, "append_reminder_ack_line", fake_append)
        monkeypatch.setattr(tasks_artist.audit, "log_event", fake_audit)
        return appended

    def test_assigned_artist_ack_is_recorded_with_stage_and_name(self, monkeypatch):
        import asyncio
        from cogs.tasks_artist import ReminderAckButton

        task = make_task()
        artist = repositories.Artist(page_id="artist-1", name="Johnny", discord_user_id="123")
        appended = self._wire(monkeypatch, task=task, artist=artist)

        interaction = self._interaction()
        asyncio.run(ReminderAckButton("task-1", "d0").callback(interaction))

        assert len(appended) == 1
        page_id, line = appended[0]
        assert page_id == "task-1"
        assert line.startswith("當天｜") and line.endswith("｜Johnny")
        reply = interaction.followup.send.await_args.args[0]
        assert "當天" in reply and "歌唱課作業" in reply

    def test_someone_else_cannot_ack_your_task(self, monkeypatch):
        import asyncio
        from cogs.tasks_artist import ReminderAckButton

        task = make_task()  # 關聯藝人只有 artist-1
        stranger = repositories.Artist(page_id="artist-9", name="路人", discord_user_id="999")
        appended = self._wire(monkeypatch, task=task, artist=stranger)

        interaction = self._interaction(user_id=999)
        asyncio.run(ReminderAckButton("task-1", "d0").callback(interaction))

        assert appended == []
        assert "不是分派給你" in interaction.followup.send.await_args.args[0]

    def test_unknown_stage_code_is_rejected(self, monkeypatch):
        import asyncio
        from cogs.tasks_artist import ReminderAckButton

        task = make_task()
        artist = repositories.Artist(page_id="artist-1", name="Johnny", discord_user_id="123")
        appended = self._wire(monkeypatch, task=task, artist=artist)

        # 用 template 抓不到的代碼也走 callback 的話，要擋下來而不是寫一行 None
        asyncio.run(ReminderAckButton("task-1", "d9").callback(self._interaction()))

        assert appended == []

    def test_reminder_view_has_button_for_stages_and_none_for_overdue(self):
        from cogs.tasks_artist import ReminderAckButton, reminder_view

        view = reminder_view("task-1", "當天")
        assert view is not None
        assert isinstance(view.children[0], ReminderAckButton)
        assert view.children[0].custom_id == "reminder_ack:task-1:d0"

        assert reminder_view("task-1", repositories.OVERDUE_STAGE) is None
