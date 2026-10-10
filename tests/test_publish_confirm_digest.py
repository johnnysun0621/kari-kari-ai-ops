"""M2-05：發文類停在「已完成」的待確認發布彙整。"""
from datetime import datetime

import pytest

import config
from services import repositories, task_notifications

TZ = config.TZ
NOW = datetime(2026, 10, 12, 10, 30, tzinfo=TZ)  # 週一
PUBLISHING = repositories.PROJECT_TYPE_PUBLISHING


def task(name, status="已完成", project_type=PUBLISHING, end=datetime(2026, 10, 1, tzinfo=TZ), artists=("a1",)):
    return repositories.TaskItem(
        page_id=name, name=name, status=status, task_type=None,
        project_types=[project_type] if project_type else [],
        artist_page_ids=list(artists), end=end,
    )


class TestSelection:
    def test_發文類已完成且截止日已過才列入(self):
        waiting = repositories.tasks_awaiting_publish_confirmation([task("A")], NOW, 3)
        assert [t.name for t in waiting] == ["A"]

    @pytest.mark.parametrize("other", [
        task("行程", project_type=repositories.PROJECT_TYPE_ITINERARY),
        task("課程", project_type=repositories.PROJECT_TYPE_COURSE_TRAINING),
        task("週任務", project_type=repositories.PROJECT_TYPE_WEEKLY),
        task("沒分類", project_type=None),
    ])
    def test_非發文類的已完成不列入(self, other):
        assert repositories.tasks_awaiting_publish_confirmation([other], NOW, 3) == []

    def test_已完成發文與進行中不列入(self):
        tasks = [task("確認過", status="已完成發文"), task("還在做", status="進行中")]
        assert repositories.tasks_awaiting_publish_confirmation(tasks, NOW, 3) == []

    def test_截止日太新的不列入_剛好滿天數就列入(self):
        recent = task("新", end=datetime(2026, 10, 10, tzinfo=TZ))   # 過了 2 天
        edge = task("剛好", end=datetime(2026, 10, 9, tzinfo=TZ))    # 過了 3 天
        waiting = repositories.tasks_awaiting_publish_confirmation([recent, edge], NOW, 3)
        assert [t.name for t in waiting] == ["剛好"]

    def test_沒有截止日的一律列入並排在最後(self):
        tasks = [task("無截止", end=None), task("有截止")]
        waiting = repositories.tasks_awaiting_publish_confirmation(tasks, NOW, 3)
        assert [t.name for t in waiting] == ["有截止", "無截止"]


class TestSchedule:
    def test_週一過了十點且今天沒發過就該發(self):
        assert task_notifications.publish_confirm_digest_due(NOW, None, 0, 10)

    def test_不是指定星期幾不發(self):
        tuesday = datetime(2026, 10, 13, 10, 30, tzinfo=TZ)
        assert not task_notifications.publish_confirm_digest_due(tuesday, None, 0, 10)

    def test_還沒到指定整點不發(self):
        early = datetime(2026, 10, 12, 9, 59, tzinfo=TZ)
        assert not task_notifications.publish_confirm_digest_due(early, None, 0, 10)

    def test_今天已發過不重複發(self):
        assert not task_notifications.publish_confirm_digest_due(NOW, "2026-10-12", 0, 10)

    def test_上週發過這週照發(self):
        assert task_notifications.publish_confirm_digest_due(NOW, "2026-10-05", 0, 10)

    def test_每天模式任何一天都可以發_但同一天只發一次(self):
        tuesday = datetime(2026, 10, 13, 11, 0, tzinfo=TZ)
        assert task_notifications.publish_confirm_digest_due(tuesday, "2026-10-12", -1, 10)
        assert not task_notifications.publish_confirm_digest_due(tuesday, "2026-10-13", -1, 10)


class TestMessage:
    def test_一則彙整含任務與藝人_不含Notion網址(self):
        message = task_notifications.publish_confirm_digest_message(
            [task("春季貼文")], {"a1": "Johnny"}, 3
        )
        assert "1 筆" in message
        assert "春季貼文" in message and "Johnny" in message
        assert "10/01" in message
        assert "已完成發文" in message
        assert "http" not in message and "notion.so" not in message and "notion.com" not in message

    def test_超過上限只列前面幾筆並註明其餘(self):
        many = [task(f"任務{i:02d}") for i in range(25)]
        message = task_notifications.publish_confirm_digest_message(many, {}, 3)
        assert "任務19" in message and "任務20" not in message
        assert "另有 5 筆" in message
        assert len(message) < 2000


class TestStateAndConfig:
    def test_最後發送日期寫進檔案_重啟後讀得回來(self, tmp_path):
        path = tmp_path / "digest.json"
        task_notifications.save_state({"last_sent_on": "2026-10-12"}, path)
        state = task_notifications.load_state(path)
        assert not task_notifications.publish_confirm_digest_due(NOW, state["last_sent_on"], 0, 10)

    def test_預設是週一十點_截止日過三天(self, monkeypatch):
        for key in ("PUBLISH_CONFIRM_DIGEST_WEEKDAY", "PUBLISH_CONFIRM_DIGEST_HOUR",
                    "PUBLISH_CONFIRM_DIGEST_AFTER_DAYS"):
            monkeypatch.delenv(key, raising=False)
        tenant = config._load_karikari_from_env()
        assert (tenant.publish_confirm_digest_weekday,
                tenant.publish_confirm_digest_hour,
                tenant.publish_confirm_digest_after_days) == (0, 10, 3)

    @pytest.mark.parametrize("raw,expected", [
        ("daily", -1), ("每天", -1), ("4", 4), ("7", 0), ("abc", 0), ("", 0),
    ])
    def test_星期幾可以設成每天或0到6_亂填退回預設(self, monkeypatch, raw, expected):
        monkeypatch.setenv("PUBLISH_CONFIRM_DIGEST_WEEKDAY", raw)
        assert config._load_karikari_from_env().publish_confirm_digest_weekday == expected

    def test_整點與天數亂填會被夾在合理範圍(self, monkeypatch):
        monkeypatch.setenv("PUBLISH_CONFIRM_DIGEST_HOUR", "99")
        monkeypatch.setenv("PUBLISH_CONFIRM_DIGEST_AFTER_DAYS", "-5")
        tenant = config._load_karikari_from_env()
        assert tenant.publish_confirm_digest_hour == 23
        assert tenant.publish_confirm_digest_after_days == 0
