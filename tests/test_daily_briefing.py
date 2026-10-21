"""每日早報（M3-24，Johnny 2026-10-20 定案方案 B）。

藝人私訊原本一個任務好幾則、同一輪一次跳好幾個階段、按完還疊一排「已收到」。
改成每天一則早報＋看細節選單＋「我都看到了」；事前提醒排進早報，不再逐則私訊。
"""
import asyncio
import dataclasses
from datetime import date, datetime
from types import SimpleNamespace

import pytest

import config
from cogs import briefing, reminders as reminders_cog, tasks_artist
from services import audit, daily_briefing, notification_log, notion_links, repositories

TZ = config.TZ
NOW = datetime(2026, 10, 20, 9, 0, tzinfo=TZ)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(notification_log, "STATE_PATH", tmp_path / "log.json")
    notification_log.reset_for_tests()
    monkeypatch.setattr(config, "TENANT", dataclasses.replace(config.TENANT, daily_briefing_enabled=True))
    yield
    notification_log.reset_for_tests()


def task(page_id, status="進行中", *, end=datetime(2026, 10, 25, tzinfo=TZ), artists=("a1",), visible=True,
         acknowledged=True, name=None, open_for_submission=False):
    return repositories.TaskItem(
        page_id=page_id, name=name or f"任務{page_id}", status=status, task_type=None,
        artist_page_ids=list(artists), discord_visible=visible, end=end,
        discord_acknowledged_at=NOW if acknowledged else None, open_for_submission=open_for_submission,
    )


# ---------- 早報要列什麼 ----------

def test_四個區塊依急迫度排序_每筆只出現一次():
    tasks = [
        task("soon"), task("fix", "需修改"), task("late", "已逾期"),
        task("new", "尚未開始", acknowledged=False),
        task("soon_and_new", "尚未開始", acknowledged=False),
    ]
    items = daily_briefing.build_items("a1", tasks, {"soon", "soon_and_new"}, {})

    assert [(i.task.page_id, i.section) for i in items] == [
        ("late", "overdue"), ("fix", "needs_fix"),
        ("soon", "due_soon"), ("soon_and_new", "due_soon"),
        ("new", "unreceived"),
    ]


def test_不列的任務():
    tasks = [
        task("done", "已完成"), task("review", "待審核"),
        task("hidden", "已逾期", visible=False),
        task("someone_else", "已逾期", artists=("a2",)),
        task("quiet"),  # 不在提醒點、已收到、沒逾期 → 今天不列
    ]
    assert daily_briefing.build_items("a1", tasks, {"review"}, {}) == []


def test_快到期只在提醒點出現():
    """佇列由截止前提醒的 loop 放進來（5／3／1 天前、當天）；不在佇列就不列。"""
    assert daily_briefing.build_items("a1", [task("t")], set(), {}) == []
    assert len(daily_briefing.build_items("a1", [task("t")], {"t"}, {})) == 1


def test_最多25筆():
    tasks = [task(f"t{i}", "已逾期") for i in range(30)]
    assert len(daily_briefing.build_items("a1", tasks, set(), {})) == daily_briefing.MAX_ITEMS


def test_逾期確認過_隔天仍列並標仍未完成():
    acks = {"late": {"a1": "2026-10-19"}}
    items = daily_briefing.build_items("a1", [task("late", "已逾期")], set(), acks)
    text = daily_briefing.briefing_text(date(2026, 10, 20), items, lambda t: "截止 10/17（已過 3 天）")

    assert "⚠️ 仍未完成（10/19 已確認）" in text


def test_當天確認過不標():
    acks = {"late": {"a1": "2026-10-20"}}
    items = daily_briefing.build_items("a1", [task("late", "已逾期")], set(), acks)
    assert "仍未完成" not in daily_briefing.briefing_text(date(2026, 10, 20), items, lambda t: "x")


def test_早報文字有區塊標題_不帶_Notion_網址():
    tasks = [task("late", "已逾期", name="MISS21 https://www.notion.so/abc"), task("soon")]
    items = daily_briefing.build_items("a1", tasks, {"soon"}, {})
    text = daily_briefing.briefing_text(date(2026, 10, 20), items, lambda t: "截止 10/25（剩 5 天）")

    assert "10/20（二）今天的任務提醒" in text
    assert "已逾期" in text and "快到期" in text and "/我的任務" in text
    assert not notion_links.contains_notion_link(text)


# ---------- 截止前提醒的 loop：早報模式不私訊藝人 ----------

class LoopHarness:
    def __init__(self, monkeypatch, tasks):
        self.dms, self.status_updates = [], []
        harness = self

        class User:
            async def send(self, content, view=None):
                harness.dms.append(content)

        class Bot:
            async def fetch_user(self, _):
                return User()

            def get_channel(self, _):
                return None

        async def get_active_tasks():
            return tasks

        async def get_artist_map():
            return {"a1": repositories.Artist(page_id="a1", name="方晨", discord_user_id="1")}

        async def noop(*args, **kwargs):
            return True

        async def update_status(page_id, status):
            harness.status_updates.append((page_id, status))

        monkeypatch.setattr(repositories, "get_active_tasks", get_active_tasks)
        monkeypatch.setattr(repositories, "get_artist_map", get_artist_map)
        monkeypatch.setattr(repositories, "append_reminder_ack_line", noop)
        monkeypatch.setattr(repositories, "update_task_status", update_status)
        monkeypatch.setattr(audit, "log_event", noop)
        monkeypatch.setattr(tasks_artist, "reminder_view", lambda *a: None)
        monkeypatch.setattr(reminders_cog, "datetime", SimpleNamespace(now=lambda tz=None: NOW))
        self.bot = Bot()

    def run(self):
        cog = SimpleNamespace(bot=self.bot, _sent={})
        cog._once = lambda rid, stage: reminders_cog.RemindersCog._once(cog, rid, stage)
        asyncio.run(reminders_cog.RemindersCog._check_task_reminders(cog))


def test_事前提醒排進早報_不私訊藝人(monkeypatch):
    state = notification_log.shared()
    state["reminders_baseline_done"] = True
    h = LoopHarness(monkeypatch, [task("t1", end=datetime(2026, 10, 22, tzinfo=TZ))])  # 剩 2 天：5、3 天前同時到

    h.run()

    assert h.dms == [], "不再一則一則私訊藝人，也不會同時跳好幾個階段"
    assert daily_briefing.queued_task_ids(notification_log.shared()) == {"t1"}


def test_逾期仍改狀態_但不私訊藝人(monkeypatch):
    state = notification_log.shared()
    state["reminders_baseline_done"] = True
    h = LoopHarness(monkeypatch, [task("t1", end=datetime(2026, 10, 1, tzinfo=TZ), open_for_submission=True)])

    h.run()

    assert h.status_updates == [("t1", "已逾期")]
    assert h.dms == []


def test_不用交件的任務過截止_不改狀態_早報列等公司確認(monkeypatch):
    """M2-14：藝人可能已經做完（例如發文），控方還沒結案；系統不自動改已逾期。"""
    state = notification_log.shared()
    state["reminders_baseline_done"] = True
    late = task("t1", end=datetime(2026, 10, 1, tzinfo=TZ))
    h = LoopHarness(monkeypatch, [late])

    h.run()

    assert h.status_updates == []
    assert h.dms == []
    items = daily_briefing.build_items("a1", [late], set(), {}, NOW)
    assert [i.section for i in items] == [daily_briefing.SECTION_AWAITING]
    text = daily_briefing.briefing_text(NOW.date(), items, lambda t: "截止 10/1（已過 20 天）")
    assert "⏳ **已過截止，等公司確認**（1）" in text
    assert daily_briefing.AWAITING_NOTE in text
    assert "已逾期" not in text


def test_控方手動改成已逾期_早報才列紅色():
    items = daily_briefing.build_items("a1", [task("t1", "已逾期", end=datetime(2026, 10, 1, tzinfo=TZ))], set(), {}, NOW)
    assert [i.section for i in items] == [daily_briefing.SECTION_OVERDUE]


# ---------- 發早報 ----------

class FakeMsg:
    def __init__(self, mid):
        self.id = mid
        self.channel = SimpleNamespace(id=900)


class BriefClient:
    def __init__(self):
        self.sent, self.deleted = [], []
        client = self

        class User:
            async def send(self, content, view=None):
                client.sent.append((content, view))
                return FakeMsg(1000 + len(client.sent))

        self.user = User()

    async def fetch_user(self, _):
        return self.user

    def get_partial_messageable(self, channel_id):
        client = self

        class Partial:
            def get_partial_message(self, message_id):
                class PM:
                    async def delete(self_inner):
                        client.deleted.append(message_id)

                return PM()

        return Partial()


@pytest.fixture
def send_env(monkeypatch):
    tasks = [task("late", "已逾期"), task("soon")]
    lines = []

    async def get_active_tasks():
        return tasks

    async def get_artist_map():
        return {"a1": repositories.Artist(page_id="a1", name="方晨", discord_user_id="1")}

    async def append(page_id, line):
        lines.append((page_id, line))
        return True

    monkeypatch.setattr(repositories, "get_active_tasks", get_active_tasks)
    monkeypatch.setattr(repositories, "get_artist_map", get_artist_map)
    monkeypatch.setattr(repositories, "append_reminder_ack_line", append)
    return SimpleNamespace(tasks=tasks, lines=lines)


def test_每位藝人一則_有選單與按鈕_佇列清掉(send_env):
    state = notification_log.shared()
    daily_briefing.queue_stages(state, "soon", ["5 天前"], NOW)
    client = BriefClient()

    stats = asyncio.run(briefing.send_daily_briefings(client, NOW))

    assert stats["sent"] == 1 and len(client.sent) == 1
    content, view = client.sent[0]
    assert "任務late" in content and "任務soon" in content
    custom_ids = {child.custom_id for child in view.children}
    assert custom_ids == {"briefing_detail:20261020", "briefing_ack:20261020"}
    assert daily_briefing.queued_task_ids(state) == set()
    assert {page for page, _ in send_env.lines} == {"late", "soon"}  # 每筆記一行「早報｜已發送」


def test_隔天發新早報前刪掉昨天的_同一天不重發(send_env):
    state = notification_log.shared()
    daily_briefing.remember_briefing(state, "1", {"day": "20261019", "channel_id": 900, "message_id": 55})
    client = BriefClient()

    asyncio.run(briefing.send_daily_briefings(client, NOW))
    asyncio.run(briefing.send_daily_briefings(client, NOW))  # 重啟後同一天再跑

    assert client.deleted == [55]
    assert len(client.sent) == 1


def test_沒有要列的任務_只清掉昨天那則(send_env):
    send_env.tasks.clear()
    state = notification_log.shared()
    daily_briefing.remember_briefing(state, "1", {"day": "20261019", "channel_id": 900, "message_id": 55})
    client = BriefClient()

    asyncio.run(briefing.send_daily_briefings(client, NOW))

    assert client.sent == [] and client.deleted == [55]


def test_我都看到了_收成一行並逐筆寫入提醒確認(send_env, monkeypatch):
    state = notification_log.shared()
    daily_briefing.remember_briefing(state, "1", {
        "day": "20261020", "channel_id": 900, "message_id": 1,
        "task_ids": ["late", "soon"], "artist_page_id": "a1", "artist_name": "方晨",
    })

    async def noop(**kwargs):
        return None

    monkeypatch.setattr(briefing.audit, "log_event", noop)
    monkeypatch.setattr(briefing, "datetime", SimpleNamespace(now=lambda tz=None: NOW.replace(minute=12),
                                                              strptime=datetime.strptime))
    edits = []

    async def edit_message(**kwargs):
        edits.append(kwargs)

    interaction = SimpleNamespace(user=SimpleNamespace(id=1), response=SimpleNamespace(edit_message=edit_message))

    asyncio.run(briefing.BriefingAckButton("20261020").callback(interaction))

    assert "✅ 已讀（09:12）" in edits[0]["content"] and edits[0]["view"] is None
    assert [(p, l) for p, l in send_env.lines] == [("late", "早報｜10/20 09:12｜方晨"), ("soon", "早報｜10/20 09:12｜方晨")]
    assert state["briefing_acks"]["late"]["a1"] == "2026-10-20"


def test_舊早報的按鈕不會誤記(send_env):
    state = notification_log.shared()
    daily_briefing.remember_briefing(state, "1", {"day": "20261020", "task_ids": ["late"]})
    replies = []

    async def send_message(content, **kwargs):
        replies.append(content)

    interaction = SimpleNamespace(user=SimpleNamespace(id=1), response=SimpleNamespace(send_message=send_message))

    asyncio.run(briefing.BriefingAckButton("20261019").callback(interaction))

    assert "之前的早報" in replies[0] and send_env.lines == []


def test_看某一筆的詳細_展開在早報同一則_不另跳訊息(monkeypatch):
    """2026-10-20 QA：原本每看一筆另跳一則，看兩筆以上往下疊，「我都看到了」被推到上面。"""
    target = dataclasses.replace(task("soon", name="week4 聽力"), description="• 聽力：50 題音程／截圖 https://www.notion.so/x")

    async def get_task(task_id):
        return target if task_id == "soon" else None

    async def find_artist(_uid):
        return repositories.Artist(page_id="a1", name="方晨", discord_user_id="1")

    monkeypatch.setattr(repositories, "get_task", get_task)
    monkeypatch.setattr(repositories, "find_artist_by_discord_id", find_artist)
    followups, edits = [], []

    async def defer(**kwargs):
        return None

    async def followup_send(content, **kwargs):
        followups.append(content)

    async def edit_original_response(**kwargs):
        edits.append(kwargs)

    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1), data={"values": ["soon"]},
        response=SimpleNamespace(defer=defer), followup=SimpleNamespace(send=followup_send),
        edit_original_response=edit_original_response,
    )
    import discord

    options = [discord.SelectOption(label="week4 聽力", value="soon"), discord.SelectOption(label="別筆", value="late")]
    asyncio.run(briefing.BriefingDetailSelect("20261020", options).callback(interaction))

    assert followups == [], "不能另外跳一則"
    embed = edits[0]["embed"]
    assert "week4 聽力" in embed.description and "50 題音程／截圖" in embed.description
    assert not notion_links.contains_notion_link(embed.description)
    view = edits[0]["view"]
    ids = [child.custom_id for child in view.children]
    # 選單與「我都看到了」留在最下面；展開某一筆時多一顆「這筆有問題」（M3-27）
    assert ids == ["briefing_detail:20261020", "briefing_ack:20261020", "task_concern:soon"]
    picked = [o for o in view.children[0].options if o.default]
    assert [o.value for o in picked] == ["soon"]

    # 同仁早報（原訊息沒有「我都看到了」）：看詳細時不冒出「我都看到了」，也沒有「這筆有問題」
    edits.clear()
    staff_message = SimpleNamespace(components=[SimpleNamespace(children=[SimpleNamespace(custom_id="briefing_detail:20261020")])])
    staff_interaction = SimpleNamespace(**{**interaction.__dict__, "message": staff_message})
    asyncio.run(briefing.BriefingDetailSelect("20261020", options).callback(staff_interaction))
    assert [child.custom_id for child in edits[0]["view"].children] == ["briefing_detail:20261020"]


def test_任務總覽的提醒確認_早報只算最近那天():
    from services import task_notifications

    t = dataclasses.replace(task("late"), reminder_ack="\n".join([
        "早報｜10/19 09:00｜已發送給 2 人",
        "早報｜10/19 09:12｜方晨",
        "早報｜10/20 09:00｜已發送給 2 人",
        "早報｜10/20 09:30｜采奕",
    ]))
    summary = task_notifications.reminder_ack_summary(t, ["方晨", "采奕"])

    assert summary == "提醒確認（10/20 早報）：1/2 ✓（采奕）"


def test_狀態檔沒有這則早報時_從訊息的選單讀任務(send_env, monkeypatch):
    async def noop(**kwargs):
        return None

    async def find_artist(_uid):
        return repositories.Artist(page_id="a1", name="方晨", discord_user_id="1")

    monkeypatch.setattr(briefing.audit, "log_event", noop)
    monkeypatch.setattr(repositories, "find_artist_by_discord_id", find_artist)
    monkeypatch.setattr(briefing, "datetime", SimpleNamespace(now=lambda tz=None: NOW, strptime=datetime.strptime))
    select = SimpleNamespace(custom_id="briefing_detail:20261020",
                             options=[SimpleNamespace(value="late"), SimpleNamespace(value="soon")])
    message = SimpleNamespace(id=5, channel=SimpleNamespace(id=900), components=[SimpleNamespace(children=[select])])
    edits = []

    async def edit_message(**kwargs):
        edits.append(kwargs)

    interaction = SimpleNamespace(user=SimpleNamespace(id=1), message=message,
                                  response=SimpleNamespace(edit_message=edit_message))

    asyncio.run(briefing.BriefingAckButton("20261020").callback(interaction))

    assert edits and "已讀" in edits[0]["content"]
    assert {p for p, _ in send_env.lines} == {"late", "soon"}


# ---------- 相關同仁每天一則（M3-25 第三階段） ----------

def test_同仁早報_只列自己有關的_沒有我都看到了(monkeypatch):
    tasks = [
        dataclasses.replace(task("late", "已逾期"), staff_page_ids=["p1"]),
        dataclasses.replace(task("soon"), staff_page_ids=["p1"]),
        dataclasses.replace(task("new", "尚未開始", acknowledged=False), staff_page_ids=["p1"]),
        task("not_mine", "已逾期"),
    ]

    async def get_active_tasks():
        return tasks

    async def get_artist_map():
        return {}

    async def get_people(ids):
        return [repositories.PersonItem(page_id="p1", name="子喬", discord_user_id="7")]

    async def append(*a):
        return True

    monkeypatch.setattr(repositories, "get_active_tasks", get_active_tasks)
    monkeypatch.setattr(repositories, "get_artist_map", get_artist_map)
    monkeypatch.setattr(repositories, "get_people_items_by_ids", get_people)
    monkeypatch.setattr(repositories, "append_reminder_ack_line", append)
    state = notification_log.shared()
    daily_briefing.queue_stages(state, "soon", ["5 天前"], NOW)
    client = BriefClient()

    stats = asyncio.run(briefing.send_daily_briefings(client, NOW))

    assert stats["staff_sent"] == 1
    content, view = client.sent[0]
    assert "任務late" in content and "任務soon" in content
    assert "任務new" not in content, "同仁不用按收到，不列那一區"
    assert "任務not_mine" not in content
    assert "相關同仁" in content
    assert [child.custom_id for child in view.children] == ["briefing_detail:20261020"]


def test_早報模式_同仁不再逐階段私訊(monkeypatch):
    state = notification_log.shared()
    state["reminders_baseline_done"] = True
    h = LoopHarness(monkeypatch, [dataclasses.replace(task("t1", end=datetime(2026, 10, 22, tzinfo=TZ)), staff_page_ids=["p1"])])
    staff_calls = []

    async def notify_staff(*a, **k):
        staff_calls.append(a)
        return 1

    async def get_people(ids):
        return [repositories.PersonItem(page_id="p1", name="子喬", discord_user_id="7")]

    monkeypatch.setattr(tasks_artist, "notify_staff", notify_staff)
    monkeypatch.setattr(repositories, "get_people_items_by_ids", get_people)

    h.run()

    assert staff_calls == [] and h.dms == []


def test_有人送失敗_重跑只補發那個人_快到期佇列保留到最後(send_env, monkeypatch):
    """M3-11：失敗時不清「快到期」佇列，重跑時補發的人一樣看得到快到期的任務；已送達的人不重複收到。"""
    send_env.tasks.append(task("t2", "已逾期", artists=("a2",)))

    async def two_artists():
        return {
            "a1": repositories.Artist(page_id="a1", name="方晨", discord_user_id="1"),
            "a2": repositories.Artist(page_id="a2", name="月夜", discord_user_id="2"),
        }

    monkeypatch.setattr(repositories, "get_artist_map", two_artists)
    state = notification_log.shared()
    daily_briefing.queue_stages(state, "soon", ["5 天前"], NOW)
    failing = {"1"}
    sent_to = []

    class Client(BriefClient):
        async def fetch_user(self, uid):
            client = self

            class User:
                async def send(self, content, view=None):
                    if str(uid) in failing:
                        raise RuntimeError("cannot DM")
                    sent_to.append((str(uid), content))
                    client.sent.append((content, view))
                    return FakeMsg(1000 + len(client.sent))

            return User()

    first = asyncio.run(briefing.send_daily_briefings(Client(), NOW, final=False))
    assert first["failed"] == 1 and [u for u, _ in sent_to] == ["2"]
    assert daily_briefing.queued_task_ids(state) == {"soon"}, "有人失敗時佇列要留給重試"

    failing.clear()
    second = asyncio.run(briefing.send_daily_briefings(Client(), NOW, final=False))
    assert second["failed"] == 0 and [u for u, _ in sent_to] == ["2", "1"], "月夜不重複收到"
    assert "任務soon" in sent_to[-1][1], "補發的早報仍有快到期的任務"
    assert daily_briefing.queued_task_ids(state) == set()


# ---------- M3-28：早報裡回報問題後，那一行標已回報，剩下的一鍵看完 ----------

class BriefMessage:
    def __init__(self, content):
        self.id, self.content = 1, content
        self.channel = SimpleNamespace(id=900)
        detail = SimpleNamespace(custom_id="briefing_detail:20261020", options=[
            SimpleNamespace(label="任務late", value="late", description="已逾期"),
            SimpleNamespace(label="任務soon", value="soon", description="快到期"),
        ])
        ack = SimpleNamespace(custom_id="briefing_ack:20261020")
        self.components = [SimpleNamespace(children=[detail]), SimpleNamespace(children=[ack])]
        self.edits = []

    async def edit(self, **kw):
        self.edits.append(kw)
        if "content" in kw:
            self.content = kw["content"]


@pytest.fixture
def reported_env(send_env, monkeypatch):
    state = notification_log.shared()
    daily_briefing.remember_briefing(state, "1", {
        "day": "20261020", "channel_id": 900, "message_id": 1,
        "task_ids": ["late", "soon"], "artist_page_id": "a1", "artist_name": "方晨",
    })

    async def noop(**kwargs):
        return None

    monkeypatch.setattr(briefing.audit, "log_event", noop)
    monkeypatch.setattr(briefing, "datetime", SimpleNamespace(now=lambda tz=None: NOW.replace(minute=12),
                                                              strptime=datetime.strptime))
    sent = []

    async def followup_send(content, **kw):
        sent.append(content)

    async def defer():
        return None

    interaction = SimpleNamespace(user=SimpleNamespace(id=1), followup=SimpleNamespace(send=followup_send),
                                  response=SimpleNamespace(defer=defer))
    text = "☀️ **10/20（二）今天的任務提醒**\n\n🔴 已逾期（1）\n・任務late｜截止 10/19\n\n⏰ 快到期（1）\n・任務soon｜截止 10/25"
    return SimpleNamespace(state=state, interaction=interaction, message=BriefMessage(text), lines=send_env.lines)


def test_早報回報一筆_那行劃掉_其他照舊(reported_env):
    env = reported_env
    remaining = asyncio.run(briefing.mark_reported(env.interaction, env.message, task("late")))
    assert remaining == 1
    content = env.message.edits[-1]["content"]
    assert "・~~任務late~~｜⚠️ 已回報問題" in content and "・任務soon｜截止 10/25" in content
    ids = [child.custom_id for child in env.message.edits[-1]["view"].children]
    assert ids == ["briefing_detail:20261020", "briefing_ack:20261020"]


def test_早報回報後其他我都看到了_回報那筆不記看過(reported_env):
    env = reported_env
    asyncio.run(briefing.mark_reported(env.interaction, env.message, task("late")))
    assert asyncio.run(briefing.ack_briefing(env.interaction, "20261020", env.message, edit_via_response=False))
    assert "✅ 已讀（09:12）" in env.message.edits[-1]["content"] and "已回報 1 筆" in env.message.edits[-1]["content"]
    assert [page for page, _ in env.lines] == ["soon"]


def test_早報每一筆都回報_直接算已讀收合(reported_env):
    env = reported_env
    asyncio.run(briefing.mark_reported(env.interaction, env.message, task("late")))
    assert asyncio.run(briefing.mark_reported(env.interaction, env.message, task("soon"))) == 0
    assert env.message.edits[-1]["view"] is None and "已回報 2 筆" in env.message.edits[-1]["content"]
    assert env.lines == []


def test_早報上回報問題_確認訊息附其他我都看到了(reported_env):
    env = reported_env
    replies = []

    async def followup_send(content, **kw):
        replies.append((content, kw.get("view")))

    interaction = SimpleNamespace(user=SimpleNamespace(id=1), followup=SimpleNamespace(send=followup_send))
    asyncio.run(tasks_artist.after_concern_reported(interaction, task("late"), env.message, None))
    content, view = replies[0]
    assert "早報裡其他的" in content and view.children[0].label == "其他我都看到了"
