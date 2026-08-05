from services import scheduling_sla


def test_collection_reminds_matching_half_day_after_deadline(tmp_path):
    scheduling_sla.reset_for_tests(tmp_path / "sla.json")
    scheduling_sla.register_collection("2026-08", created_ts=100, fill_deadline_ts=1000)

    assert scheduling_sla.due_events(1000 + 12 * 3600 - 1) == []
    events = scheduling_sla.due_events(1000 + 12 * 3600)
    assert [event.key for event in events] == ["collection:2026-08:1000:match_due"]

    scheduling_sla.mark_event_sent(events[0].key)
    assert scheduling_sla.due_events(1000 + 13 * 3600) == []


def test_matched_collection_does_not_remind(tmp_path):
    scheduling_sla.reset_for_tests(tmp_path / "sla.json")
    scheduling_sla.register_collection("2026-08", created_ts=100, fill_deadline_ts=1000)
    scheduling_sla.mark_pool_matched("2026-08", matched_ts=900)

    assert scheduling_sla.due_events(999999) == []


def test_task_emits_teacher_48h_and_72h_events_until_completed(tmp_path):
    scheduling_sla.reset_for_tests(tmp_path / "sla.json")
    scheduling_sla.register_task(
        "task-1", "2026-08", created_ts=1000, teacher_deadline_ts=1000 + 24 * 3600,
    )

    keys = {event.key for event in scheduling_sla.due_events(1000 + 72 * 3600)}
    assert keys == {
        "task:task-1:teacher_overdue",
        "task:task-1:sla_48h",
        "task:task-1:stale_72h",
    }

    scheduling_sla.mark_completed("task-1", 1000 + 73 * 3600)
    assert scheduling_sla.due_events(1000 + 80 * 3600) == []


def test_teacher_response_suppresses_only_teacher_deadline(tmp_path):
    scheduling_sla.reset_for_tests(tmp_path / "sla.json")
    scheduling_sla.register_task(
        "task-1", "2026-08", created_ts=1000, teacher_deadline_ts=1000 + 24 * 3600,
    )
    scheduling_sla.mark_teacher_responded("task-1", 2000)

    keys = {event.key for event in scheduling_sla.due_events(1000 + 48 * 3600)}
    assert keys == {"task:task-1:sla_48h"}
