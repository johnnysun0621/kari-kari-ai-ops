"""M6-11 小律記憶：稱呼、記得什麼、忘掉、每天整理、每週週報、控方指令。"""
import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

import config
from cogs import channel_guard
from cogs import course_qa as course_qa_cog
from services import repositories, ritsu_chat, ritsu_memory, ritsu_voice

TZ = config.TZ


def artist(page_id="a1", name="辰葳 (測試）"):
    fields = repositories.Artist.__dataclass_fields__
    kwargs = {"page_id": page_id, "name": name}
    for key, value in {"discord_user_id": "11", "status": "", "drive_folder_url": ""}.items():
        if key in fields:
            kwargs[key] = value
    return repositories.Artist(**kwargs)


# ---------- 藝人說的話 ----------

@pytest.mark.parametrize("text,expected", [
    ("小律可以叫我喵喵不要叫我辰葳嗎", "喵喵"),
    ("以後叫我小喵就好", "小喵"),
    ("你可以叫我阿明嗎", "阿明"),
    ("叫我「Mimi」", "Mimi"),
    ("不要叫我辰葳", None),
    ("明天早上叫我起床", None),
    ("你都叫我做什麼", None),
    ("這週有什麼任務", None),
])
def test_聽得懂改稱呼(text, expected):
    assert ritsu_memory.parse_nickname_request(text) == expected


@pytest.mark.parametrize("text", ["小律你記得我什麼", "你知道我哪些事", "你對我有什麼印象"])
def test_問記得什麼(text):
    assert ritsu_memory.is_recall_request(text)


@pytest.mark.parametrize("text,expected", [
    ("忘掉", True), ("小律忘掉我說的", True), ("把我說的都忘掉吧", True), ("清掉記憶", True),
    ("我忘記交作業了", False), ("忘記密碼怎麼辦", False), ("這週有什麼任務", False),
])
def test_聽得懂要忘掉(text, expected):
    assert ritsu_memory.is_forget_request(text) is expected


# ---------- 存與讀 ----------

def test_稱呼記住後_每個地方都用():
    a = artist()
    assert ritsu_memory.call_name_for(a) == "辰葳"
    ritsu_memory.set_nickname(a, "喵喵")
    assert ritsu_memory.call_name_for(a) == "喵喵"
    saved = json.loads(ritsu_memory.STATE_PATH.read_text(encoding="utf-8"))
    assert saved["artists"]["a1"]["nickname"] == "喵喵" and saved["artists"]["a1"]["artist_name"] == "辰葳 (測試）"


def test_敏感的不記_太長和重複的也不記():
    notes = ritsu_memory.clean_notes([
        "正在練吉他", "喜歡抽菸，七星藍莓", "最近跟男友分手", "正在練吉他", "x" * 50, "常問這週任務",
        "喜歡簡短的回答", "想學編曲", "喜歡日系樂團", "第六行",
    ])
    assert notes == ["正在練吉他", "常問這週任務", "喜歡簡短的回答", "想學編曲", "喜歡日系樂團"]


def test_忘掉就全部清掉():
    a = artist()
    ritsu_memory.set_nickname(a, "喵喵")
    ritsu_memory.set_notes(a, ["正在練吉他"])
    assert ritsu_memory.clear("a1") is True
    assert ritsu_memory.call_name_for(a) == "辰葳" and not ritsu_memory.has_memory("a1")


@pytest.mark.real_voice  # 這個測試要跑真的 ritsu_chat.reply（HTTP 由下面自己假造）
def test_記憶帶進聊天():
    a = artist()
    ritsu_memory.set_nickname(a, "喵喵")
    ritsu_memory.set_notes(a, ["正在練吉他"])
    text = ritsu_memory.memory_text("a1")
    assert "喵喵" in text and "正在練吉他" in text
    seen = {}

    async def ask(system, user, **kw):
        seen["user"] = user
        seen["system"] = system
        return {"reply": "好呀～"}

    original = ritsu_voice._ask_json
    ritsu_voice._ask_json = ask
    try:
        asyncio.run(ritsu_chat.reply(
            call_name="喵喵", question="吉他怎麼進步", tasks=[], now=datetime(2026, 11, 8, 12, tzinfo=TZ),
            deadline_text=lambda t: "", convo=ritsu_chat.Conversation(day=datetime(2026, 11, 8).date()),
            state={}, memory_text=text,
        ))
    finally:
        ritsu_voice._ask_json = original
    assert "<memory>" in seen["user"] and "正在練吉他" in seen["user"]
    assert "菸酒" in seen["system"], "要告訴小律哪些不記"


def test_整理筆記_AI寫了敏感的也會被擋(monkeypatch):
    async def ask(system, user, **kw):
        assert "絕對不記" in system and "正在練吉他" in user
        return {"notes": ["正在練吉他", "喜歡七星藍莓香菸", "常問下個月行程"]}

    monkeypatch.setattr(ritsu_voice, "_ask_json", ask)
    notes = asyncio.run(ritsu_memory.summarize(["正在練吉他"], "藝人：我喜歡抽菸", state={}, now=datetime.now(TZ)))
    assert notes == ["正在練吉他", "常問下個月行程"]


def test_整理時AI用不了_保留舊筆記(monkeypatch):
    async def ask(*a, **kw):
        return None

    monkeypatch.setattr(ritsu_voice, "_ask_json", ask)
    assert asyncio.run(ritsu_memory.summarize(["正在練吉他"], "藝人：嗨", state={}, now=datetime.now(TZ))) is None


# ---------- 小律怎麼回 ----------

def make_cog(monkeypatch, found=None):
    cog = course_qa_cog.CourseQACog.__new__(course_qa_cog.CourseQACog)
    cog._answer_locks = {}
    cog._chat_memory = ritsu_chat.Memory()
    cog._thread_by_user = {}
    found = found if found is not None else artist()

    async def find(discord_id):
        return found

    monkeypatch.setattr(repositories, "find_artist_by_discord_id", find)
    return cog


def test_叫我喵喵_當下記住並回覆(monkeypatch):
    cog = make_cog(monkeypatch)
    answer = asyncio.run(cog._memory_answer(SimpleNamespace(id=11), "小律可以叫我喵喵不要叫我辰葳嗎"))
    assert "喵喵同學" in answer and "早報" in answer
    assert ritsu_memory.get("a1")["nickname"] == "喵喵"


def test_問記得什麼_列出來(monkeypatch):
    cog = make_cog(monkeypatch)
    ritsu_memory.set_nickname(artist(), "喵喵")
    ritsu_memory.set_notes(artist(), ["正在練吉他"])
    answer = asyncio.run(cog._memory_answer(SimpleNamespace(id=11), "小律你記得我什麼"))
    assert "叫你喵喵" in answer and "正在練吉他" in answer and "忘掉" in answer


def test_還沒記得什麼_老實說(monkeypatch):
    cog = make_cog(monkeypatch)
    answer = asyncio.run(cog._memory_answer(SimpleNamespace(id=11), "你記得我什麼"))
    assert "還沒有記住" in answer and "叫我○○" in answer


def test_忘掉_清掉並改回本名(monkeypatch):
    cog = make_cog(monkeypatch)
    ritsu_memory.set_nickname(artist(), "喵喵")
    answer = asyncio.run(cog._memory_answer(SimpleNamespace(id=11), "忘掉"))
    assert "清掉" in answer and "辰葳同學" in answer
    assert not ritsu_memory.has_memory("a1")


def test_一般問題不攔(monkeypatch):
    cog = make_cog(monkeypatch)
    assert asyncio.run(cog._memory_answer(SimpleNamespace(id=11), "這週有什麼任務")) is None


# ---------- 每天半夜整理 ----------

def test_半夜整理_有新對話的才整理_一天一次(monkeypatch):
    cog = make_cog(monkeypatch)
    calls = []

    async def turns(since, until):
        calls.append((since, until))
        return {"11": [("artist", "我在練吉他"), ("ritsu", "加油！")]}

    async def summarize(old, transcript, **kw):
        assert "藝人：我在練吉他" in transcript
        return ["正在練吉他"]

    cog._thread_turns = turns
    monkeypatch.setattr(ritsu_memory, "summarize", summarize)
    asyncio.run(cog._run_nightly_memory(datetime(2026, 11, 9, 2, 30, tzinfo=TZ)))
    assert calls == [], "3 點前不整理"
    asyncio.run(cog._run_nightly_memory(datetime(2026, 11, 9, 3, 0, tzinfo=TZ)))
    asyncio.run(cog._run_nightly_memory(datetime(2026, 11, 9, 3, 30, tzinfo=TZ)))
    assert len(calls) == 1, "同一天只整理一次"
    assert ritsu_memory.get("a1")["notes"] == ["正在練吉他"]
    asyncio.run(cog._run_nightly_memory(datetime(2026, 11, 10, 3, 0, tzinfo=TZ)))
    assert calls[1][0] == datetime(2026, 11, 9, 3, 0, tzinfo=TZ), "接著上次整理到的時間"


# ---------- 每週週報 ----------

def test_找出答不出來的題目():
    turns = [
        ("artist", "這週有什麼任務"), ("ritsu", "這週有 2 筆"),
        ("artist", "交件連結是什麼"), ("ritsu", "練習作業的繳交連結小律這裡沒有資料，請向公司確認喔。"),
        ("artist", "通告費什麼時候發"), ("ritsu", "這個小律不能代表公司回答，請直接問公司喔"),
    ]
    assert ritsu_memory.unanswered_questions(turns) == ["交件連結是什麼", "通告費什麼時候發"]


def test_週報內容():
    text = ritsu_memory.weekly_report_text(
        datetime(2026, 11, 2, tzinfo=TZ), datetime(2026, 11, 8, tzinfo=TZ),
        [ritsu_memory.ArtistWeek("喵喵", 5, ["交件連結是什麼"]), ritsu_memory.ArtistWeek("月夜", 2, [])],
        feedback_count=0,
    )
    assert "11/2～11/8" in text and "2 位藝人問了 7 次" in text
    assert "答不出來、或請他問公司：1 次" in text and "喵喵：「交件連結是什麼」" in text
    assert "回報問題」：0 次" in text and "不會自己改規則" in text


def test_週報_沒人問也發():
    text = ritsu_memory.weekly_report_text(datetime(2026, 11, 2, tzinfo=TZ), datetime(2026, 11, 8, tzinfo=TZ), [], None)
    assert "這週沒有藝人問小律" in text and "回報問題" not in text


def test_週報_週一10點後發一次_發到營運內部通知(monkeypatch):
    cog = make_cog(monkeypatch)
    cog.bot = None
    sent = []

    async def turns(since, until):
        assert (since, until) == (datetime(2026, 11, 2, tzinfo=TZ), datetime(2026, 11, 9, tzinfo=TZ))
        return {"11": [("artist", "交件連結是什麼"), ("ritsu", "請向公司確認喔")]}

    async def count(since):
        return 1

    async def try_send(channel, **kw):
        sent.append((channel, kw["content"]))
        return object()

    cog._thread_turns = turns
    monkeypatch.setattr(repositories, "count_feedback_since", count)
    monkeypatch.setattr(channel_guard, "ops_channel", lambda bot: "ops")
    monkeypatch.setattr(channel_guard, "try_send", try_send)
    asyncio.run(cog._run_weekly_report(datetime(2026, 11, 9, 9, 30, tzinfo=TZ)))
    assert sent == [], "10 點前不發"
    asyncio.run(cog._run_weekly_report(datetime(2026, 11, 9, 10, 0, tzinfo=TZ)))
    asyncio.run(cog._run_weekly_report(datetime(2026, 11, 9, 10, 30, tzinfo=TZ)))
    assert len(sent) == 1 and sent[0][0] == "ops"
    assert "11/2～11/8" in sent[0][1] and "辰葳：「交件連結是什麼」" in sent[0][1]
    asyncio.run(cog._run_weekly_report(datetime(2026, 11, 10, 10, 0, tzinfo=TZ)))
    assert len(sent) == 1, "只在週一發"


# ---------- 控方指令 ----------

def test_控方看記憶與清除():
    ritsu_memory.set_nickname(artist(), "喵喵")
    ritsu_memory.set_notes(artist(), ["正在練吉他"])
    entries = ritsu_memory.artists_with_memory()
    view = course_qa_cog.RitsuMemoryView(entries)
    select = view.children[0]
    assert select.placeholder == "選擇要看的藝人…" and select.options[0].label == "辰葳 (測試）（喵喵）"
    log = []

    async def edit_message(**kw):
        log.append(kw)

    interaction = SimpleNamespace(response=SimpleNamespace(edit_message=edit_message))
    select._values = ["a1"]
    asyncio.run(select.callback(interaction))
    assert "稱呼：喵喵" in log[0]["content"] and "正在練吉他" in log[0]["content"]
    clear_button = next(item for item in log[0]["view"].children if getattr(item, "label", "") == "清除這位藝人的記憶")
    asyncio.run(clear_button.callback(interaction))
    assert "已清除" in log[1]["content"] and not ritsu_memory.has_memory("a1")
