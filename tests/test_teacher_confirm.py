"""Teacher confirmation link and page helper tests."""

from dataclasses import replace

import config
from services import teacher_confirm
from web import pages


def _set_teacher_config(monkeypatch, *, secret="unit-test-secret", base_url=""):
    tenant = replace(
        config.get_tenant(),
        teacher_link_secret=secret,
        teacher_link_base_url=base_url,
    )
    monkeypatch.setattr(config, "TENANT", tenant)
    return tenant


def test_signed_token_verifies_and_becomes_one_time(monkeypatch):
    _set_teacher_config(monkeypatch)
    token = teacher_confirm.sign_token("request-1", expires_at_ts=2000)

    check = teacher_confirm.verify_token(token, now_ts=1000)

    assert check.ok
    assert check.request_page_id == "request-1"
    assert check.expires_at_ts == 2000

    teacher_confirm.mark_token_used(token, check.expires_at_ts)
    used = teacher_confirm.verify_token(token, now_ts=1001)

    assert not used.ok
    assert used.error == "used"


def test_token_rejects_tampering_and_expiry(monkeypatch):
    _set_teacher_config(monkeypatch)
    token = teacher_confirm.sign_token("request-1", expires_at_ts=2000)

    assert teacher_confirm.verify_token(token + "x", now_ts=1000).error == "invalid"
    assert teacher_confirm.verify_token(token, now_ts=2001).error == "expired"


def test_task_form_token_keeps_task_payload_signed(monkeypatch):
    _set_teacher_config(monkeypatch)
    token = teacher_confirm.sign_task_token(
        "task-1",
        expires_at_ts=2000,
        task_name="八月歌唱課",
        course_type="歌唱課",
        duration_text="1 小時",
        teacher="王老師",
        windows=["8/1 13:00-14:00", "8/2 15:00-16:00"],
    )

    check = teacher_confirm.verify_token(token, now_ts=1000)

    assert check.ok
    assert check.request_page_id == "task-1"
    assert check.payload["kind"] == "task"
    assert check.payload["windows"] == ["8/1 13:00-14:00", "8/2 15:00-16:00"]


def test_teacher_result_format_uses_one_window_per_line():
    selected = ["8/1（週六）13:30–16:00", "8/12（週三）13:30–16:00"]
    message = "老師可行時段：\n" + "\n".join(f"・{window}" for window in selected)

    assert "・8/1（週六）13:30–16:00\n・8/12（週三）13:30–16:00" in message


def test_confirm_url_uses_configured_base_url(monkeypatch):
    _set_teacher_config(monkeypatch, base_url="https://example.com/")

    assert teacher_confirm.can_generate_links()
    assert teacher_confirm.confirm_url("abc") == "https://example.com/confirm/abc"


def test_confirm_page_escapes_user_controlled_text():
    html = pages.confirm_page_html(
        request_name="<script>alert(1)</script>",
        course_type="歌唱課",
        duration_text="1 小時",
        teacher="<b>老師</b>",
        windows=[(0, "8/1 10:00-11:00")],
        action_url="/confirm/token",
    )

    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;b&gt;老師&lt;/b&gt;" in html
    assert 'type=\'checkbox\'' in html
