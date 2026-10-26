"""2026-10-26 全面掃描找到的問題（M1-11、M2-15、M3-31、M4-19、M6-09）。"""
import asyncio
import dataclasses
from datetime import date, datetime, timedelta

import config
from cogs.submissions import SubmissionsCog, review_dm_text
from services import ops_briefing, repositories

TZ = config.TZ


def _sub(**kw):
    base = dict(page_id="s1", name="方晨｜編曲作業｜10/26 10:00", status="需修改",
                artist_page_ids=["a1"], task_page_ids=["t1"])
    base.update(kw)
    return repositories.SubmissionItem(**base)


# ---------- M4-19 審核 ----------

def test_老師也可以當審核人員(monkeypatch):
    async def people(ids):
        return [
            repositories.PersonItem("p1", "老師甲", "111", "老師"),
            repositories.PersonItem("p2", "同仁乙", "222", "公司成員"),
            repositories.PersonItem("p3", "廠商丙", "333", "合作廠商"),
        ]
    monkeypatch.setattr(repositories, "get_people_items_by_ids", people)
    got = asyncio.run(repositories.get_people_by_ids(["p1", "p2", "p3"]))
    assert [p.name for p in got] == ["老師甲", "同仁乙"]


def test_需修改私訊寫出修改期限():
    deadline = datetime(2026, 10, 29, 10, 0, tzinfo=TZ)
    text = review_dm_text(_sub(), task_name="編曲作業", result="需修改", revision_deadline=deadline)
    assert "修改期限：10/29（四）" in text
    assert "修改期限" not in review_dm_text(_sub(), task_name="編曲作業", result="通過", revision_deadline=deadline)


def _patch_task_writes(monkeypatch, existing_deadline):
    written = {}

    async def update_status(task_id, status):
        written["status"] = status

    async def set_deadline(task_id, deadline):
        written["deadline"] = deadline

    async def brief(task_id):
        return repositories.TaskItem(page_id=task_id, name="編曲作業", status="待審核", task_type=None,
                                     revision_deadline=existing_deadline)

    monkeypatch.setattr(repositories, "update_task_status", update_status)
    monkeypatch.setattr(repositories, "set_task_revision_deadline", set_deadline)
    monkeypatch.setattr(repositories, "get_task_brief", brief)
    return written


def test_Notion審成需修改沒填期限_自動補3天(monkeypatch):
    written = _patch_task_writes(monkeypatch, None)
    before = datetime.now(TZ)
    shown = asyncio.run(SubmissionsCog._update_review_task_status(None, _sub(), "需修改"))
    assert written["status"] == "需修改"
    assert shown == written["deadline"]
    assert timedelta(days=3) <= shown - before < timedelta(days=3, minutes=1)


def test_Notion審成需修改有填期限_沿用不覆蓋(monkeypatch):
    existing = datetime(2026, 11, 2, tzinfo=TZ)
    written = _patch_task_writes(monkeypatch, existing)
    shown = asyncio.run(SubmissionsCog._update_review_task_status(None, _sub(), "需修改"))
    assert shown == existing and "deadline" not in written


def test_Discord審核照填的天數(monkeypatch):
    written = _patch_task_writes(monkeypatch, None)
    shown = asyncio.run(SubmissionsCog._update_review_task_status(None, _sub(), "退回", revision_days=5))
    assert shown == written["deadline"] and (shown - datetime.now(TZ)).days in (4, 5)


def test_通過清掉修改期限(monkeypatch):
    written = _patch_task_writes(monkeypatch, datetime(2026, 11, 2, tzinfo=TZ))
    shown = asyncio.run(SubmissionsCog._update_review_task_status(None, _sub(status="通過"), "通過"))
    assert shown is None and written == {"status": "已完成", "deadline": None}


def test_審核私訊用任務名稱不用提交名稱(monkeypatch):
    _patch_task_writes(monkeypatch, None)
    assert asyncio.run(SubmissionsCog._review_task_name(None, _sub())) == "編曲作業"

    async def missing(task_id):
        return None
    monkeypatch.setattr(repositories, "get_task_brief", missing)
    assert asyncio.run(SubmissionsCog._review_task_name(None, _sub())) == "方晨｜編曲作業｜10/26 10:00"


# ---------- M6-09 使用回饋不貼公告區 ----------

def test_使用回饋不同步到公告區_已貼的收回(monkeypatch):
    from cogs import bulletin

    class Msg:
        def __init__(self, mid):
            self.id = mid
            self.deleted = False

        async def delete(self):
            self.deleted = True

        async def edit(self, **kw):
            pass

    old = Msg(500)

    class Channel:
        sent = []

        async def fetch_message(self, mid):
            return old if mid == 500 else Msg(mid)

        async def send(self, **kw):
            self.sent.append(kw)
            return Msg(600 + len(self.sent))

    channel = Channel()
    bot = type("Bot", (), {"get_channel": lambda self, _: channel, "add_view": lambda self, *a, **k: None})()
    cog = bulletin.BulletinCog.__new__(bulletin.BulletinCog)
    cog.bot = bot
    cog._registered_message_ids = set()

    def item(pid, category):
        return repositories.BulletinItem(page_id=pid, url="", title=pid, category=category, status="未處理", date=None)

    async def items():
        return [item("fb", repositories.FEEDBACK_CATEGORY), item("news", "📢 公告")]

    saved = {}
    monkeypatch.setattr(repositories, "get_bulletin_items", items)
    monkeypatch.setattr(bulletin, "_load_state", lambda: {"fb": {"message_id": "500", "fingerprint": "x"}})
    monkeypatch.setattr(bulletin, "_save_state", lambda s: saved.update(s))
    monkeypatch.setattr(bulletin, "bulletin_channel_id", lambda: 1)

    created, updated, skipped = asyncio.run(cog.sync_bulletins())

    assert old.deleted and "fb" not in saved
    assert created == 1 and len(channel.sent) == 1 and "news" in saved


# ---------- M3-31／M4-19 營運早報 ----------

def test_營運早報待審核時數照設定():
    line = ops_briefing.OpsLine("t1", "x", "待審核沒人負責")
    b = ops_briefing.OpsBriefing(review_unassigned=[line], review_after_hours=36)
    assert "📮 **待審核超過 36 小時，沒人負責**" in ops_briefing.briefing_text(date(2026, 10, 26), b)


def test_審核人私訊不到的任務排進營運早報_送出後清掉():
    state = {}
    ops_briefing.record_unreachable_review(state, "t1")
    ops_briefing.record_unreachable_review(state, "t1")
    assert ops_briefing.unreachable_review_ids(state) == {"t1"}
    ops_briefing.clear_queues(state)
    assert ops_briefing.unreachable_review_ids(state) == set()


# ---------- M1-11 排課 ----------

def test_派發有人收到就算排課完成_不再發時效假警報():
    from cogs import tasks_artist
    from services import notification_log, scheduling_sla

    start = int(datetime(2026, 10, 1, tzinfo=TZ).timestamp())
    scheduling_sla.register_task("t1", "2026-11", start)
    task = repositories.TaskItem(page_id="t1", name="錄音", status="尚未開始", task_type="錄音")
    bot = type("Bot", (), {"get_channel": lambda self, _: None})()
    asyncio.run(tasks_artist._after_first_delivery(bot, task, notification_log.shared()))
    later = start + 100 * 3600
    assert [e for e in scheduling_sla.due_events(later) if "t1" in e.key] == []


def test_沒有進行中的收集_藝人補填不發假的收齊通知(monkeypatch):
    from cogs import channel_guard, scheduling_artist
    from services import time_collection

    sent = []

    class Channel:
        async def send(self, content):
            sent.append(content)

    monkeypatch.setattr(channel_guard, "ops_channel", lambda client: Channel())
    monkeypatch.setattr(time_collection, "active_collections", lambda: [])
    artist = repositories.Artist(page_id="a1", name="方晨", discord_user_id="1")
    interaction = type("I", (), {"client": None})()

    asyncio.run(scheduling_artist._send_pool_completion_notifications(interaction, artist, "2026-11", 999))

    assert sent == []


def test_時間收集說明寫實際的按鈕():
    from services import time_collection

    sop = time_collection.availability_selection_sop()
    assert "檢查全部時段" in sop and "全部確認送出" in sop and "按「完成」" not in sop
