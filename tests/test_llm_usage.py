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


def test_record_tracks_tenant_and_artist_totals(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_usage, "STATE_PATH", tmp_path / "usage.json")

    llm_usage.record({"total_tokens": 120}, user_key="artist-a")
    llm_usage.record({"total_tokens": 80}, user_key="artist-b")

    assert llm_usage.daily_total() == 200
    assert llm_usage.daily_total("artist-a") == 120
    assert llm_usage.daily_total("artist-b") == 80


def test_limit_status_checks_system_and_artist_limits(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_usage, "STATE_PATH", tmp_path / "usage.json")
    tenant = llm_usage.config.get_tenant()
    monkeypatch.setattr(
        llm_usage.config,
        "get_tenant",
        lambda: type("Tenant", (), {
            "name": tenant.name,
            "tz": tenant.tz,
            "llm_daily_token_limit": 1000,
            "llm_per_artist_daily_token_limit": 100,
        })(),
    )

    llm_usage.record({"total_tokens": 100}, user_key="artist-a")

    status = llm_usage.limit_status("artist-a")
    assert status.allowed is False
    assert status.artist_remaining == 0
    assert status.system_remaining == 900


def test_legacy_integer_state_remains_readable(monkeypatch, tmp_path):
    state_path = tmp_path / "usage.json"
    monkeypatch.setattr(llm_usage, "STATE_PATH", state_path)
    day = llm_usage._today()
    state_path.write_text(
        '{"KariKari": {"' + day + '": 321}}',
        encoding="utf-8",
    )

    assert llm_usage.daily_total() == 321
    assert llm_usage.daily_total("artist-a") == 0
