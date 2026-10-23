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
    monkeypatch.setattr(ritsu_voice, "review_line", review)


def pytest_configure(config):
    config.addinivalue_line("markers", "real_voice: 這個測試要跑真的 ritsu_voice.daily_voice（但 HTTP 仍要自己假造）")
