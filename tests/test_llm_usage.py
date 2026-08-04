from services import llm_usage


def test_record_accumulates_usage(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_usage, "STATE_PATH", tmp_path / "usage.json")
    assert llm_usage.record({"total_tokens": 120}) == 120
    assert llm_usage.record({"total_tokens": 80}) == 200
    assert llm_usage.daily_total() == 200


def test_missing_usage_does_not_change_total(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_usage, "STATE_PATH", tmp_path / "usage.json")
    assert llm_usage.record({}) == 0
    assert llm_usage.daily_total() == 0
