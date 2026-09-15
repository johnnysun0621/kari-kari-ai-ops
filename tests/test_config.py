"""config.py helper tests."""

import config


class TestEnvInt:
    def test_missing_env_uses_default(self, monkeypatch):
        monkeypatch.delenv("KKA_TEST_INT", raising=False)

        assert config._env_int("KKA_TEST_INT", default=7) == 7

    def test_numeric_env_is_parsed(self, monkeypatch):
        monkeypatch.setenv("KKA_TEST_INT", "123")

        assert config._env_int("KKA_TEST_INT") == 123

    def test_placeholder_env_uses_default(self, monkeypatch):
        monkeypatch.setenv("KKA_TEST_INT", "你的伺服器ID")

        assert config._env_int("KKA_TEST_INT", default=0) == 0


class TestOperationalTimings:
    """營運節奏的時間值必須能從 .env 調整，不可寫死在程式裡。

    這三項是不同公司偏好會不同的值；寫死的話換租戶就得改程式。
    """

    TIMING_ENV_KEYS = [
        "TASK_ACK_REMINDER_HOURS",
        "REMIND_BEFORE_DEADLINE_HOURS",
        "FILE_UPLOAD_WAIT_MINUTES",
        "REMINDER_INTERVAL_MINUTES",
        "REVIEW_REMINDER_AFTER_HOURS",
        "REVIEW_REMINDER_INTERVAL_HOURS",
        "BULLETIN_SYNC_INTERVAL_MINUTES",
    ]

    def test_每個時間設定都讀得到環境變數(self, monkeypatch):
        import inspect

        source = inspect.getsource(config._load_karikari_from_env)
        for key in self.TIMING_ENV_KEYS:
            assert f'"{key}"' in source, f"{key} 應該從環境變數讀取，不要寫死"

    def test_預設值維持目前的營運節奏(self, monkeypatch):
        for key in self.TIMING_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        tenant = config._load_karikari_from_env()

        assert tenant.task_ack_reminder_hours == 12
        assert tenant.remind_before_deadline_hours == 6
        assert tenant.file_upload_wait_minutes == 180
        assert tenant.reminder_interval_minutes == 30
        assert tenant.review_reminder_after_hours == 24
        assert tenant.review_reminder_interval_hours == 12
        assert tenant.bulletin_sync_interval_minutes == 5
