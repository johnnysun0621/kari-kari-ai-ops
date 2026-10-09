"""services/drive_api.py 的憑證選擇邏輯測試。

不打真的 Google API——`_get_service()` 裡的 googleapiclient `build()` 與
google-auth 的 Credentials 建構子都用 monkeypatch 換成假的，只驗證「選對憑證
路徑、傳對參數」，不驗證 Drive API 本身的行為（那是 google 套件的責任）。
"""
from types import SimpleNamespace

import pytest

from services import drive_api


@pytest.fixture(autouse=True)
def _reset_service_cache():
    """每個測試前後都清掉模組級快取，避免測試互相汙染（跟 `_get_service()` 的
    cache-by-path 設計本身一樣，key 不同就不會撞到，但保險起見還是清空）。"""
    drive_api.reset_for_tests()
    yield
    drive_api.reset_for_tests()


class TestIsConfigured:
    def test_neither_file_exists_is_not_configured(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_KEY_PATH", str(tmp_path / "sa.json"))
        monkeypatch.setenv("GOOGLE_OAUTH_TOKEN_PATH", str(tmp_path / "token.json"))

        assert drive_api.is_configured() is False

    def test_only_service_account_file_is_configured(self, tmp_path, monkeypatch):
        sa_path = tmp_path / "sa.json"
        sa_path.write_text("{}")
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_KEY_PATH", str(sa_path))
        monkeypatch.setenv("GOOGLE_OAUTH_TOKEN_PATH", str(tmp_path / "token.json"))

        assert drive_api.is_configured() is True

    def test_only_oauth_token_is_configured(self, tmp_path, monkeypatch):
        token_path = tmp_path / "token.json"
        token_path.write_text("{}")
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_KEY_PATH", str(tmp_path / "sa.json"))
        monkeypatch.setenv("GOOGLE_OAUTH_TOKEN_PATH", str(token_path))

        assert drive_api.is_configured() is True


class TestCredentialPriority:
    """服務帳號優先於 OAuth——即使兩個檔案都在，也要走服務帳號，
    這樣從 OAuth 切換過去時不必先刪舊 token.json。"""

    def test_service_account_wins_when_both_exist(self, tmp_path, monkeypatch):
        sa_path = tmp_path / "sa.json"
        token_path = tmp_path / "token.json"
        sa_path.write_text("{}")
        token_path.write_text("{}")
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_KEY_PATH", str(sa_path))
        monkeypatch.setenv("GOOGLE_OAUTH_TOKEN_PATH", str(token_path))

        assert drive_api._credential_path() == str(sa_path)

    def test_falls_back_to_oauth_when_no_service_account_file(self, tmp_path, monkeypatch):
        token_path = tmp_path / "token.json"
        token_path.write_text("{}")
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_KEY_PATH", str(tmp_path / "sa.json"))
        monkeypatch.setenv("GOOGLE_OAUTH_TOKEN_PATH", str(token_path))

        assert drive_api._credential_path() == str(token_path)


class TestGetServiceUsesTheRightCredentialType:
    def test_service_account_file_present_uses_service_account_credentials(
        self, tmp_path, monkeypatch,
    ):
        sa_path = tmp_path / "sa.json"
        sa_path.write_text("{}")
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_KEY_PATH", str(sa_path))
        monkeypatch.setenv("GOOGLE_OAUTH_TOKEN_PATH", str(tmp_path / "token.json"))

        calls = {"service_account": 0, "oauth_user": 0}

        def fake_from_service_account_file(path, scopes):
            calls["service_account"] += 1
            assert path == str(sa_path)
            assert scopes == drive_api._DRIVE_SCOPES
            return SimpleNamespace()

        def fake_from_authorized_user_file(path):
            calls["oauth_user"] += 1
            return SimpleNamespace()

        import google.oauth2.service_account
        import google.oauth2.credentials
        monkeypatch.setattr(
            google.oauth2.service_account.Credentials,
            "from_service_account_file",
            staticmethod(fake_from_service_account_file),
        )
        monkeypatch.setattr(
            google.oauth2.credentials.Credentials,
            "from_authorized_user_file",
            staticmethod(fake_from_authorized_user_file),
        )
        monkeypatch.setattr(
            "googleapiclient.discovery.build",
            lambda *a, **k: SimpleNamespace(),
        )

        drive_api._get_service()

        assert calls == {"service_account": 1, "oauth_user": 0}

    def test_only_oauth_token_present_uses_oauth_user_credentials(self, tmp_path, monkeypatch):
        """舊有行為不能因為新增服務帳號支援而壞掉——只有 token.json 時照舊走 OAuth。"""
        token_path = tmp_path / "token.json"
        token_path.write_text("{}")
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_KEY_PATH", str(tmp_path / "sa.json"))
        monkeypatch.setenv("GOOGLE_OAUTH_TOKEN_PATH", str(token_path))

        calls = {"service_account": 0, "oauth_user": 0}

        def fake_from_service_account_file(path, scopes):
            calls["service_account"] += 1
            return SimpleNamespace()

        def fake_from_authorized_user_file(path):
            calls["oauth_user"] += 1
            assert path == str(token_path)
            return SimpleNamespace()

        import google.oauth2.service_account
        import google.oauth2.credentials
        monkeypatch.setattr(
            google.oauth2.service_account.Credentials,
            "from_service_account_file",
            staticmethod(fake_from_service_account_file),
        )
        monkeypatch.setattr(
            google.oauth2.credentials.Credentials,
            "from_authorized_user_file",
            staticmethod(fake_from_authorized_user_file),
        )
        monkeypatch.setattr(
            "googleapiclient.discovery.build",
            lambda *a, **k: SimpleNamespace(),
        )

        drive_api._get_service()

        assert calls == {"service_account": 0, "oauth_user": 1}

    def test_service_cached_by_credential_path(self, tmp_path, monkeypatch):
        """同一組憑證路徑第二次呼叫不該重新建構 service——跟原本 _services 快取的用意一樣。"""
        sa_path = tmp_path / "sa.json"
        sa_path.write_text("{}")
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_KEY_PATH", str(sa_path))
        monkeypatch.setenv("GOOGLE_OAUTH_TOKEN_PATH", str(tmp_path / "token.json"))

        build_calls = {"n": 0}

        import google.oauth2.service_account
        monkeypatch.setattr(
            google.oauth2.service_account.Credentials,
            "from_service_account_file",
            staticmethod(lambda path, scopes: SimpleNamespace()),
        )

        def fake_build(*a, **k):
            build_calls["n"] += 1
            return SimpleNamespace()

        monkeypatch.setattr("googleapiclient.discovery.build", fake_build)

        first = drive_api._get_service()
        second = drive_api._get_service()

        assert first is second
        assert build_calls["n"] == 1
