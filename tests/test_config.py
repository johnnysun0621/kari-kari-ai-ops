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
