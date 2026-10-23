"""M3-30：小律每天依藝人狀況寫的開頭和結尾。HTTP 一律假造，不打真的 OpenAI。"""
import asyncio
import dataclasses
import json
from datetime import datetime

import httpx
import pytest

import config
from services import ritsu_voice

NOW = datetime(2026, 10, 23, 9, 0, tzinfo=config.TZ)
pytestmark = pytest.mark.real_voice


@pytest.fixture(autouse=True)
def with_key(monkeypatch):
    monkeypatch.setattr(config, "TENANT", dataclasses.replace(config.TENANT, llm_api_key="test-key", llm_model="gpt-5.6-luna"))


def fake_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def reply(opening, closing, tokens=120):
    body = {"choices": [{"message": {"content": json.dumps({"opening": opening, "closing": closing}, ensure_ascii=False)}}],
            "usage": {"total_tokens": tokens}}
    return lambda request: httpx.Response(200, json=body)


def run(counts, handler, state=None):
    state = {} if state is None else state
    return asyncio.run(ritsu_voice.daily_voice(counts, state=state, now=NOW, client=fake_client(handler))), state


def test_用_AI_寫的兩句_並記下用量():
    voice, state = run(ritsu_voice.Counts(overdue=1), reply("昨天那筆小律陪你解決！", "加油喔！"))
    assert (voice.opening, voice.closing, voice.source) == ("昨天那筆小律陪你解決！", "加油喔！", "ai")
    assert state["ritsu_voice"] == {"day": "2026-10-23", "tokens": 120}


def test_只送狀況統計_不送名字和任務名稱():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return reply("好", "好")(request)

    run(ritsu_voice.Counts(unreceived=2, due_soon=1, yesterday_read=True), handler)
    user = seen["messages"][1]["content"]
    assert "還沒按收到的任務：2 筆" in user and "快到期：1 筆" in user and "昨天的早報有看完" in user
    assert seen["reasoning_effort"] == "none" and seen["response_format"] == {"type": "json_object"}
    assert "小律" in seen["messages"][0]["content"], "人設檔有送出"


@pytest.mark.parametrize("opening", [
    "請看 https://notion.so/x",
    "請看 HTTPS://EXAMPLE.COM",
    "@everyone 早安",
    "# 今天加油",
    "**今天加油**",
    "太長" * 40,
    "",
])
def test_寫壞的句子_改用範本句(opening):
    counts = ritsu_voice.Counts(due_soon=2)
    voice, _ = run(counts, reply(opening, "加油！"))
    assert voice.opening == ritsu_voice.template_voice(counts).opening
    assert voice.closing == "加油！" and voice.source == "partial"


@pytest.mark.parametrize("opening", [
    "今天有 99 筆逾期，一起加油！",
    "週末前一起把事情整理好！",
    "有任務逾期了，小律陪你！",
])
def test_數字_具體時間或與狀況矛盾的事實_改用範本句(opening):
    counts = ritsu_voice.Counts(due_soon=1)
    voice, _ = run(counts, reply(opening, "一起加油！"))
    assert voice.opening == ritsu_voice.template_voice(counts).opening
    assert voice.closing == "一起加油！" and voice.source == "partial"


def test_與狀況一致但不自行報數字的句子可以使用():
    voice, _ = run(ritsu_voice.Counts(due_soon=1), reply("有任務快到期了，小律陪你！", "一起加油！"))
    assert voice.opening == "有任務快到期了，小律陪你！"
    assert voice.source == "ai"


def test_API_出錯或逾時_改用範本句_不丟例外():
    counts = ritsu_voice.Counts(overdue=2)

    def boom(request):
        raise httpx.ReadTimeout("slow")

    voice, _ = run(counts, boom)
    assert voice == ritsu_voice.template_voice(counts)
    voice, _ = run(counts, lambda r: httpx.Response(500, text="oops"))
    assert voice.source == "template"


def test_超過今天的額度就不呼叫(monkeypatch):
    monkeypatch.setenv("RITSU_VOICE_DAILY_TOKEN_LIMIT", "100")
    called = []

    def handler(request):
        called.append(1)
        return reply("好", "好")(request)

    voice, _ = run(ritsu_voice.Counts(), handler, state={"ritsu_voice": {"day": "2026-10-23", "tokens": 100}})
    assert called == [] and voice.source == "template"


def test_沒有_API_key_直接用範本句(monkeypatch):
    monkeypatch.setattr(config, "TENANT", dataclasses.replace(config.TENANT, llm_api_key=""))
    voice, _ = run(ritsu_voice.Counts(unreceived=1), lambda r: pytest.fail("不該呼叫"))
    assert voice.source == "template" and "收到" in voice.opening


def test_稱呼拿掉括號():
    assert ritsu_voice.call_name("Johnny (測試）") == "Johnny"
    assert ritsu_voice.call_name("方晨") == "方晨"


# ---------- M4-18：審核結果 ----------

def test_審核結果一句_只送數字():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        body = {"choices": [{"message": {"content": json.dumps({"line": "連續三次一次就過，好厲害！"}, ensure_ascii=False)}}],
                "usage": {"total_tokens": 300}}
        return httpx.Response(200, json=body)

    facts = ritsu_voice.ReviewFacts(result="通過", attempt=1, pass_streak=3, recent_passed=5, recent_fixed=1)
    line = asyncio.run(ritsu_voice.review_line(facts, state={}, now=NOW, client=fake_client(handler)))
    assert line == "連續三次一次就過，好厲害！"
    user = seen["messages"][1]["content"]
    assert "審核結果：通過" in user and "連續一次就通過：3 件" in user


def test_審核結果_AI_失敗用範本句():
    def boom(request):
        raise httpx.ConnectError("down")

    facts = ritsu_voice.ReviewFacts(result="需修改", attempt=2)
    assert asyncio.run(ritsu_voice.review_line(facts, state={}, now=NOW, client=fake_client(boom))) == "再調整一下就好，小律陪你一起改。"
