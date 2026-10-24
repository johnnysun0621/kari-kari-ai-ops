"""M6-08：跟小律聊天。HTTP 一律假造，不打真的 OpenAI。"""
import asyncio
import dataclasses
import json
from datetime import date, datetime

import httpx
import pytest

import config
from services import repositories, ritsu_chat

NOW = datetime(2026, 10, 24, 10, 0, tzinfo=config.TZ)
pytestmark = pytest.mark.real_voice


@pytest.fixture(autouse=True)
def with_key(monkeypatch):
    monkeypatch.setattr(config, "TENANT", dataclasses.replace(config.TENANT, llm_api_key="test-key", llm_model="gpt-5.6-luna"))


def task(name, status="進行中"):
    return repositories.TaskItem(page_id=name, name=name, status=status, task_type="行政",
                                 discord_acknowledged_at=None if status == "尚未開始" else NOW)


def run(handler, *, question="好累喔", convo=None, list_text=None, tasks=None):
    convo = convo or ritsu_chat.Conversation(day=NOW.date())
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return asyncio.run(ritsu_chat.reply(
        call_name="方晨", question=question, tasks=tasks if tasks is not None else [task("編曲作業"), task("拍短片", "尚未開始")],
        now=NOW, deadline_text=lambda t: "截止 10/25（剩 1 天）", convo=convo, state={}, list_text=list_text, client=client,
    ))


def answer(text):
    body = {"choices": [{"message": {"content": json.dumps({"reply": text}, ensure_ascii=False)}}], "usage": {"total_tokens": 900}}
    return lambda request: httpx.Response(200, json=body)


def test_帶上今天的任務與最近的對話():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return answer("辛苦了方晨同學！編曲作業明天要交，小律陪你一起加油～")(request)

    convo = ritsu_chat.Conversation(day=NOW.date())
    convo.add("artist", "這週有什麼任務")
    convo.add("ritsu", "這週有兩筆喔")
    text = run(handler, convo=convo)
    assert text.startswith("辛苦了方晨同學")
    user = seen["messages"][1]["content"]
    assert "編曲作業" in user and "拍短片｜行政｜截止 10/25（剩 1 天）｜尚未開始｜還沒按收到" in user
    assert "藝人：這週有什麼任務" in user and "小律：這週有兩筆喔" in user and "藝人現在說：好累喔" in user
    assert "<list>" not in user
    assert "小律" in seen["messages"][0]["content"]


def test_清單模式_告訴小律清單會接在下面():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return answer("這週有兩筆，先從編曲作業開始吧！")(request)

    run(handler, question="這週有什麼任務", list_text="1. 編曲作業｜行政｜截止 10/25")
    assert "<list>" in seen["messages"][1]["content"] and "清單上面的開場" in seen["messages"][0]["content"]


@pytest.mark.parametrize("bad", ["看這裡 https://notion.so/x", "", "@everyone 早安", "字" * 800])
def test_回覆有網址或太長_清掉或退回(bad):
    text = run(answer(bad))
    assert text is None or ("http" not in text and len(text) <= ritsu_chat.MAX_REPLY_CHARS)


def test_API_失敗回_None():
    def boom(request):
        raise httpx.ReadTimeout("slow")

    assert run(boom) is None


def test_記憶跨日清空_只記最近幾則():
    memory = ritsu_chat.Memory()
    convo = memory.get(1, date(2026, 10, 24))
    for i in range(15):
        convo.add("artist", f"第{i}句")
    assert len(convo.turns) == ritsu_chat.HISTORY_TURNS and convo.turns[0][1] == "第5句"
    assert memory.get(1, date(2026, 10, 24)) is convo
    assert not memory.get(1, date(2026, 10, 25)).turns, "隔天重新開始"
