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
    )


def test_notification_message_explains_task_and_submission_method():
    task = make_task(end=datetime(2026, 8, 5, 18, 0, tzinfo=config.TZ))

    message = task_notifications.notification_message(task)

    assert "歌唱課作業" in message
    assert "08/05 18:00" in message
    assert "檔案" in message
    assert "收到任務" in message


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
