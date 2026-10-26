"""全部測試共用：絕不打真的 OpenAI。

.env 有設 LLM_API_KEY 時，早報的小律問候（M3-30）會真的呼叫 API：測試變慢、結果不固定、還會花錢。
這裡一律換成固定範本句；要測 API 那段的測試自己再 monkeypatch（見 test_ritsu_voice.py）。
"""
import pytest

from services import ritsu_voice


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch, request):
    if request.node.get_closest_marker("real_voice"):
        return

    async def template(counts, *, state, now, client=None):
        return ritsu_voice.template_voice(counts)

    async def review(facts, *, state, now, client=None):
        return ritsu_voice.review_template(facts)

    monkeypatch.setattr(ritsu_voice, "daily_voice", template)
    async def query(facts, *, state, now, client=None):
        return ritsu_voice.query_template(facts)

    async def no_chat(**kwargs):
        return None  # 小律聊天（M6-08）一律退回固定回答

    from services import ritsu_chat

    monkeypatch.setattr(ritsu_chat, "reply", no_chat)
    monkeypatch.setattr(ritsu_voice, "review_line", review)
    monkeypatch.setattr(ritsu_voice, "query_line", query)


@pytest.fixture(autouse=True)
def no_real_state_files(tmp_path, monkeypatch):
    """測試絕不碰 bot 正在用的 .state：派發會呼叫 scheduling_sla.mark_completed（M1-11），
    通知紀錄也一律寫到暫存資料夾。各測試自己的 fixture 可以再覆蓋。"""
    from services import notification_log, scheduling_sla

    monkeypatch.setattr(notification_log, "STATE_PATH", tmp_path / "task_notification_log.json")
    notification_log.reset_for_tests()
    monkeypatch.setattr(scheduling_sla, "_STATE_PATH", tmp_path / "scheduling_sla.json")
    scheduling_sla.reset_for_tests()
    yield
    notification_log.reset_for_tests()
    scheduling_sla.reset_for_tests()


def pytest_configure(config):
    config.addinivalue_line("markers", "real_voice: 這個測試要跑真的 ritsu_voice.daily_voice（但 HTTP 仍要自己假造）")
