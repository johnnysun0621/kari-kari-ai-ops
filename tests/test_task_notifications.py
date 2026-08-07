from datetime import datetime

import config
from services import repositories, task_notifications


def make_task(
    *,
    page_id: str = "task-1",
    name: str = "歌唱課作業",
    submit_method: str | None = "檔案",
    end: datetime | None = None,
    discord_notified_at: datetime | None = None,
    discord_acknowledged_at: datetime | None = None,
) -> repositories.TaskItem:
    return repositories.TaskItem(
        page_id=page_id,
        name=name,
        status="尚未開始",
        task_type="課程與作業",
        submit_method=submit_method,
        end=end,
        end_has_time=end is not None,
        artist_page_ids=["artist-1"],
        discord_visible=True,
        discord_notified_at=discord_notified_at,
        discord_acknowledged_at=discord_acknowledged_at,
    )


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


def test_missing_submit_method_shows_task_description_instead_of_placeholder():
    task = make_task(submit_method=None)
    task.description = "請完成舞蹈練習，並於課後提交錄影。"

    message = task_notifications.notification_message(task)

    assert "提交方式：請依任務說明" not in message
    assert "提交方式／任務說明" in message
    assert "請完成舞蹈練習，並於課後提交錄影。" in message


def test_public_announcement_uses_configured_task_types():
    course = make_task()
    public = make_task(name="八月拍攝")
    public.task_type = "拍攝"

    assert not task_notifications.is_public_announcement_task(course, ("通告", "演出", "拍攝"))
    assert task_notifications.is_public_announcement_task(public, ("通告", "演出", "拍攝"))


def test_acknowledgement_reminder_lists_each_unconfirmed_task():
    tasks = [make_task(page_id="task-a", name="歌唱課作業"), make_task(page_id="task-b", name="舞蹈課作業")]

    message = task_notifications.acknowledgement_reminder_message(tasks)

    assert "歌唱課作業" in message
    assert "舞蹈課作業" in message
    assert "重新送出任務通知" in message


def test_resend_list_excludes_received_tasks_but_keeps_unconfirmed_tasks():
    pending = make_task(page_id="pending")
    concern = make_task(page_id="concern")
    received = make_task(
        page_id="received",
        discord_acknowledged_at=datetime(2026, 8, 2, 12, 0, tzinfo=config.TZ),
    )

    assert task_notifications.is_resendable_task(pending)
    assert task_notifications.is_resendable_task(concern)
    assert not task_notifications.is_resendable_task(received)


def test_acknowledgement_scan_runs_on_fixed_two_hour_interval():
    now = datetime(2026, 8, 2, 14, 0, tzinfo=config.TZ)

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


def test_acknowledgement_props_store_the_received_time():
    received_at = datetime(2026, 8, 2, 12, 30, tzinfo=config.TZ)

    props = task_notifications.acknowledgement_props(received_at)

    assert props["Discord 收到時間"]["date"]["start"] == received_at.isoformat()
