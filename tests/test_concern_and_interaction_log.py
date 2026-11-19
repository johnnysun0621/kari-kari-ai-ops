"""M3-32：互動失敗查得到＋「這筆不行」先選原因、按下去一定 3 秒內跳視窗。"""
import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord

import bot as bot_module
from cogs import channel_guard, tasks_artist
from services import interaction_log, repositories, task_bundle

TZ = ZoneInfo("Asia/Taipei")
ARTIST = repositories.Artist(page_id="a1", name="方晨", discord_user_id="100")


def _task(page_id="t1", *, status="尚未開始", visible=True):
    return repositories.TaskItem(
        page_id=page_id, name="11/24（二）發布：極限Y2K 2", status=status, task_type="發文",
        artist_page_ids=["a1"], discord_visible=visible,
        end=datetime(2026, 11, 24, tzinfo=TZ), end_has_time=False,
    )


# ---------- 原因文字 ----------

def test_選原因加補充():
    assert task_bundle.concern_reason_text("想延期", " 想延到11/25 ") == "想延期：想延到11/25"


def test_只選原因不補充():
    assert task_bundle.concern_reason_text("時間撞到", "") == "時間撞到"
    assert task_bundle.concern_reason_text("其他", "  ") == "其他" + task_bundle.CONCERN_NO_DETAIL


def test_沒選原因只用補充_兩個都空回空字串():
    assert task_bundle.concern_reason_text("", "需要延期") == "需要延期"
    assert task_bundle.concern_reason_text("", "") == ""


# ---------- 視窗長相 ----------

def _modal_items(modal):
    labels = [child for child in modal.children if isinstance(child, discord.ui.Label)]
    return [(label.text, label.component) for label in labels]


def test_視窗先選原因_補充可以不填():
    modal = tasks_artist.TaskConcernModal("t1")
    (reason_label, select), (detail_label, text) = _modal_items(modal)
    assert reason_label == task_bundle.CONCERN_REASON_LABEL
    assert isinstance(select, discord.ui.Select) and select.required
    assert [o.label for o in select.options] == [label for label, _ in task_bundle.CONCERN_REASONS]
    assert detail_label == task_bundle.CONCERN_DETAIL_LABEL
    assert isinstance(text, discord.ui.TextInput) and text.required is False


# ---------- 按下去：最多等 1.5 秒 ----------

def _button_interaction():
    calls = {}

    async def send_message(content, **kw):
        calls["message"] = content

    async def send_modal(modal):
        calls["modal"] = modal

    return SimpleNamespace(response=SimpleNamespace(send_message=send_message, send_modal=send_modal)), calls


def test_Notion太慢_不等了直接跳視窗(monkeypatch):
    async def slow(_tid):
        await asyncio.sleep(5)

    monkeypatch.setattr(repositories, "get_task_brief", slow)
    monkeypatch.setattr(tasks_artist, "CONCERN_PRECHECK_SECONDS", 0.05)
    interaction, calls = _button_interaction()
    asyncio.run(tasks_artist.open_concern_modal(interaction, "t1"))
    assert isinstance(calls.get("modal"), tasks_artist.TaskConcernModal)


def test_從早報按這筆不行_走到跳視窗(monkeypatch):
    """實際路徑：早報展開後那顆「這筆不行」（TaskConcernButton）→ 跳視窗。"""
    async def brief(_tid):
        return _task()

    monkeypatch.setattr(repositories, "get_task_brief", brief)
    interaction, calls = _button_interaction()
    asyncio.run(tasks_artist.TaskConcernButton("t1", "這筆不行").callback(interaction))
    assert isinstance(calls.get("modal"), tasks_artist.TaskConcernModal)


# ---------- 送出：一定再檢查 ----------

def _submit(monkeypatch, task, *, choice=None, detail=""):
    calls = {"sent": [], "feedback": None, "ops": None, "returned": False}

    async def get_task(_tid):
        return task

    async def find_artist(_uid):
        return ARTIST

    async def feedback(_tid, entry):
        calls["feedback"] = entry

    async def returned(_tid):
        calls["returned"] = True

    async def try_send(_channel, **kw):
        calls["ops"] = kw["content"]

    async def after(*_args):
        return None

    async def log_event(**_kw):
        return None

    monkeypatch.setattr(repositories, "get_task", get_task)
    monkeypatch.setattr(repositories, "find_artist_by_discord_id", find_artist)
    monkeypatch.setattr(repositories, "append_task_feedback", feedback)
    monkeypatch.setattr(repositories, "return_task_after_artist_concern", returned)
    monkeypatch.setattr(tasks_artist.audit, "log_event", log_event)
    monkeypatch.setattr(channel_guard, "try_send", try_send)
    monkeypatch.setattr(channel_guard, "ops_channel", lambda _client: None)
    monkeypatch.setattr(tasks_artist, "after_concern_reported", after)

    async def defer(**_kw):
        return None

    async def followup_send(content, **_kw):
        calls["sent"].append(content)

    modal = tasks_artist.TaskConcernModal("t1")
    modal.choice._values = [choice] if choice else []
    modal.reason._value = detail
    interaction = SimpleNamespace(user=SimpleNamespace(id=100), client=None, response=SimpleNamespace(defer=defer),
                                  followup=SimpleNamespace(send=followup_send))
    asyncio.run(modal.on_submit(interaction))
    return calls


def test_只選想延期不補充_也能送出_公司看得到原因(monkeypatch):
    calls = _submit(monkeypatch, _task(), choice="想延期")
    assert "回饋：想延期" in calls["feedback"]
    assert "回饋：想延期" in calls["ops"] and calls["returned"] is True


def test_選原因加補充_公司看到完整一行(monkeypatch):
    calls = _submit(monkeypatch, _task(), choice="想延期", detail="想延到11/25")
    assert "回饋：想延期：想延到11/25" in calls["ops"]


def test_送出時已經回報過_說明已回報_不重複寫(monkeypatch):
    calls = _submit(monkeypatch, _task(visible=False), choice="時間撞到")
    assert calls["sent"] == [task_bundle.ALREADY_REPORTED_REPLY]
    assert calls["feedback"] is None and calls["ops"] is None


def test_送出時已經交件_說明不能用按鈕(monkeypatch):
    calls = _submit(monkeypatch, _task(status="待審核"), choice="時間撞到")
    assert calls["sent"] == [task_bundle.CONCERN_BLOCKED_REPLY] and calls["feedback"] is None


# ---------- 互動紀錄 ----------

def _interaction(kind, data, *, guild_id=None, message_id=None):
    return SimpleNamespace(
        type=kind, data=data, user=SimpleNamespace(name="fangchen", id=100), guild_id=guild_id, channel_id=55,
        message=SimpleNamespace(id=message_id) if message_id else None,
    )


def test_記按鈕_誰按了什麼在哪裡():
    line = interaction_log.describe(_interaction(
        discord.InteractionType.component, {"component_type": 2, "custom_id": "task_concern:t1"}, message_id=999,
    ))
    assert line == "收到互動：按鈕 task_concern:t1｜fangchen（100）｜私訊｜訊息 999"


def test_記選單選了哪幾個_太多只列前三個():
    line = interaction_log.describe(_interaction(
        discord.InteractionType.component,
        {"component_type": 3, "custom_id": "briefing_detail:20261119", "values": ["a", "b", "c", "d"]},
        guild_id=1,
    ))
    assert "選單 briefing_detail:20261119＝a、b、c 等 4 個" in line and "頻道 55" in line


def test_填寫視窗只記是哪個視窗_不記內容():
    data = {"custom_id": "abc", "components": [{"components": [{"value": "我真的沒有時間拍了"}]}]}
    line = interaction_log.describe(_interaction(discord.InteractionType.modal_submit, data), "TaskConcernModal")
    assert "填寫視窗送出 TaskConcernModal" in line and "沒有時間" not in line


def test_找不到視窗也照記():
    line = interaction_log.describe(_interaction(discord.InteractionType.modal_submit, {"custom_id": "abc"}))
    assert "bot 這邊找不到這個視窗" in line


def test_記指令_不記自動完成():
    assert "指令 /我的任務" in interaction_log.describe(
        _interaction(discord.InteractionType.application_command, {"name": "我的任務"}))
    assert interaction_log.describe(_interaction(discord.InteractionType.autocomplete, {"name": "x"})) is None


def test_查視窗名稱失敗不影響():
    interaction = _interaction(discord.InteractionType.modal_submit, {"custom_id": "abc"})
    assert interaction_log.modal_name_for(object(), interaction) == ""


def test_bot收到互動就寫log(caplog):
    interaction = _interaction(discord.InteractionType.component, {"component_type": 2, "custom_id": "briefing_ack:20261119"})
    with caplog.at_level("INFO", logger="karikari.interaction"):
        asyncio.run(bot_module.KariKariBot.on_interaction(None, interaction))
    assert any("briefing_ack:20261119" in r.getMessage() for r in caplog.records)
