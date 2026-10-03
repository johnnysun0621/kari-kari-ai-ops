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


def test_task_emits_48h_and_72h_events_until_completed(tmp_path):
    scheduling_sla.reset_for_tests(tmp_path / "sla.json")
    scheduling_sla.register_task("task-1", "2026-08", created_ts=1000)

    keys = {event.key for event in scheduling_sla.due_events(1000 + 72 * 3600)}
    assert keys == {
        "task:task-1:sla_48h",
        "task:task-1:stale_72h",
    }

    scheduling_sla.mark_completed("task-1", 1000 + 73 * 3600)
    assert scheduling_sla.due_events(1000 + 80 * 3600) == []


def test_old_state_file_with_removed_teacher_fields_still_loads(tmp_path):
    """舊狀態檔留有已移除的老師確認欄位時，載入不應該整個炸掉。"""
    state_path = tmp_path / "sla.json"
    state_path.write_text(
        '{"collections": [], "tasks": [{"task_page_id": "task-1", "month": "2026-08",'
        ' "started_ts": 1000, "teacher_deadline_ts": 5000, "teacher_responded_ts": 0,'
        ' "completed_ts": 0}], "sent_events": []}',
        encoding="utf-8",
    )
    scheduling_sla.reset_for_tests(state_path)

    keys = {event.key for event in scheduling_sla.due_events(1000 + 48 * 3600)}
    assert keys == {"task:task-1:sla_48h"}


class TestDeregisterCollection:
    """收集被取消時，SLA 追蹤也要移除，否則 48／72 小時後還會發升級提醒。"""

    def test_removes_collection_and_stops_sla_events(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scheduling_sla, "_STATE_PATH", tmp_path / "sla.json")
        scheduling_sla._loaded = False
        scheduling_sla._collections.clear()
        scheduling_sla._tasks.clear()

        scheduling_sla.register_collection("2026-09", created_ts=1000, fill_deadline_ts=2000)
        assert scheduling_sla.deregister_collection("2026-09", 2000) is True

        scheduling_sla._loaded = False
        scheduling_sla._collections.clear()
        # 重新載入後也不該再出現，代表確實寫回檔案
        assert scheduling_sla.deregister_collection("2026-09", 2000) is False

    def test_unknown_collection_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scheduling_sla, "_STATE_PATH", tmp_path / "sla.json")
        scheduling_sla._loaded = False
        scheduling_sla._collections.clear()

        assert scheduling_sla.deregister_collection("2099-01", 1) is False
