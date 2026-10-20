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
         acknowledged=True, name=None):
    return repositories.TaskItem(
        page_id=page_id, name=name or f"任務{page_id}", status=status, task_type=None,
        artist_page_ids=list(artists), discord_visible=visible, end=end,
        discord_acknowledged_at=NOW if acknowledged else None,
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
    h = LoopHarness(monkeypatch, [task("t1", end=datetime(2026, 10, 1, tzinfo=TZ))])

    h.run()

    assert h.status_updates == [("t1", "已逾期")]
    assert h.dms == []


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


def test_看某一筆的詳細_只有自己看得到_不帶網址(monkeypatch):
    target = dataclasses.replace(task("soon", name="week4 聽力"), description="• 聽力：50 題音程／截圖 https://www.notion.so/x")

    async def get_task(task_id):
        return target if task_id == "soon" else None

    async def find_artist(_uid):
        return repositories.Artist(page_id="a1", name="方晨", discord_user_id="1")

    monkeypatch.setattr(repositories, "get_task", get_task)
    monkeypatch.setattr(repositories, "find_artist_by_discord_id", find_artist)
    sent = []

    async def defer(**kwargs):
        return None

    async def followup_send(content, **kwargs):
        sent.append((content, kwargs))

    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1), data={"values": ["soon"]},
        response=SimpleNamespace(defer=defer), followup=SimpleNamespace(send=followup_send),
    )
    asyncio.run(briefing.BriefingDetailSelect("20261020").callback(interaction))

    content, kwargs = sent[0]
    assert kwargs.get("ephemeral") is True
    assert "week4 聽力" in content and "50 題音程／截圖" in content
    assert not notion_links.contains_notion_link(content)


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
