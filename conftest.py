# 讓 pytest 把專案根目錄加入 sys.path
import pytest


@pytest.fixture(autouse=True)
def _isolated_time_collection_state(tmp_path, monkeypatch):
    """所有測試共用：時間收集狀態檔導向 tmp_path，避免測試寫入真實 .state/。"""
    from services import time_collection

    monkeypatch.setattr(time_collection, "_STATE_PATH", tmp_path / "time_collection.json")
    time_collection.clear_active_collections_for_tests()
    yield
    time_collection.clear_active_collections_for_tests()
