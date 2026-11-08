"""2026-11-08 讀藝人跟小律的實際對話後修的三件事。"""
import asyncio
from types import SimpleNamespace

import pytest

from cogs import course_qa as course_qa_cog
from services import ritsu_chat, task_query


def test_一句裡有兩個第N個_取最後一個():
    assert task_query.index_reference("第一個已經交了 然後幫我確認第二個的完整資料") == 2
    assert task_query.index_reference("第三筆的內容") == 3
    assert task_query.index_reference("這週有什麼任務") is None


@pytest.mark.parametrize("question", [
    "現在有什麼任務 本周練習繳交作業連結是什麼",
    "作業要怎麼交",
    "檔案要去哪裡上傳",
    "提交入口在哪",
])
def test_問怎麼交件(question):
    assert task_query.asks_how_to_submit(question)


@pytest.mark.parametrize("question", ["第一個已經交了", "這週有什麼任務", "我還有哪些沒交", "11月有什麼任務"])
def test_不是在問怎麼交件(question):
    assert not task_query.asks_how_to_submit(question)


def make_cog(answer):
    cog = course_qa_cog.CourseQACog.__new__(course_qa_cog.CourseQACog)
    cog._answer_locks = {}
    cog._chat_memory = ritsu_chat.Memory()

    async def unlocked(user, question):
        return answer

    cog._answer_for_user_unlocked = unlocked
    return cog


def test_問交件連結_回答後面接上交件步驟():
    cog = make_cog("🤖 **小律**\n\n📑 這週的任務有 2 筆\n…\n\n要交件或按「收到任務」，請用 `/我的任務`。")
    answer = asyncio.run(cog._answer_for_user(SimpleNamespace(id=1), "本周練習繳交作業連結是什麼"))
    assert "交作業的方法" in answer and "提交任務" in answer and "我已上傳到 Drive" in answer


def test_沒問交件_不多接():
    cog = make_cog("🤖 **小律**\n\n你好～")
    answer = asyncio.run(cog._answer_for_user(SimpleNamespace(id=1), "你好"))
    assert "交作業的方法" not in answer


def test_小律不能答應做不到的事():
    rules = ritsu_chat._RULES
    assert "做不到的事絕對不要答應" in rules and "轉告公司" in rules
    assert "叫我○○" in rules, "換稱呼要請他用小律記憶（M6-11）"
    assert "菸酒" in rules and "不要說記住了" in rules
    assert "不要叫他問公司" in rules


def test_歡迎詞和入口都不說只有你看得到():
    assert "只有你和公司管理員看得到" in course_qa_cog.thread_welcome()
    assert "只有你和公司管理員看得到" in course_qa_cog.panel_text()
