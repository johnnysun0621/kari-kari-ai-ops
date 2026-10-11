"""M6-02：月度排程固定公告區。"""
import asyncio
from datetime import date, datetime
from types import SimpleNamespace

import discord
import pytest

import config
from cogs import bulletin as bulletin_cog
from services import monthly_board, repositories
from services.repositories import Artist, TaskItem

TZ = config.TZ
NOW = datetime(2026, 10, 11, 14, 30, tzinfo=TZ)
PUBLIC = ("通告", "演出")


def task(page_id, task_type="演出", start=datetime(2026, 10, 14, 14, 0, tzinfo=TZ),
         end=datetime(2026, 10, 14, 16, 0, tzinfo=TZ), visible=True, status="進行中"):
    return TaskItem(page_id=page_id, name=f"任務{page_id}", status=status, task_type=task_type,
                    artist_page_ids=["a1"], discord_visible=visible, start=start, end=end,
                    start_has_time=True, end_has_time=True)


class TestSelection:
    def test_只收當月_已派發_公開類型_未取消封存(self):
        tasks = [
            task("ok"), task("錄音", task_type="錄音"), task("草稿", visible=False),
            task("封存", status="已封存"), task("下月", start=datetime(2026, 11, 2, tzinfo=TZ), end=None),
            task("通告", task_type="通告", start=datetime(2026, 10, 2, 10, 0, tzinfo=TZ)),
        ]
        picked = monthly_board.board_tasks(tasks, NOW.date(), PUBLIC)
        assert [t.page_id for t in picked] == ["通告", "ok"]  # 依時間排序

    def test_已完成的任務仍列在當月排程(self):
        assert monthly_board.board_tasks([task("1", status="已完成")], NOW.date(), PUBLIC)


class TestRender:
    def test_顯示日期星期時段類型與藝人_不含網址(self):
        lines = monthly_board.board_lines([task("1")], {"a1": "辰葳"})
        assert lines == ["• 10/14（三） 14:00–16:00｜演出｜任務1｜辰葳"]
        text = monthly_board.render(NOW.date(), lines, PUBLIC, NOW)
        assert "2026 年 10 月排程" in text and "http" not in text

    def test_沒有排程也有一則訊息(self):
        assert "本月目前沒有公開排程" in monthly_board.render(NOW.date(), [], PUBLIC, NOW)

    def test_太長會截斷並註明筆數(self):
        lines = [f"• 10/{i:02d}（一）｜演出｜{'很長的任務名稱' * 5}" for i in range(1, 80)]
        text = monthly_board.render(NOW.date(), lines, PUBLIC, NOW)
        assert len(text) <= 2000 and "另有" in text

    def test_指紋不含更新時間(self):
        assert monthly_board.fingerprint(["a"]) == monthly_board.fingerprint(["a"])
        assert monthly_board.fingerprint(["a"]) != monthly_board.fingerprint(["b"])


class FakeMessage:
    def __init__(self, mid, content):
        self.id = mid
        self.content = content
        self.edits = 0

    async def edit(self, content=None, **kw):
        self.content = content
        self.edits += 1


class FakeChannel:
    def __init__(self):
        self.messages = {}
        self.sent = 0

    async def send(self, content):
        self.sent += 1
        msg = FakeMessage(100 + self.sent, content)
        self.messages[msg.id] = msg
        return msg

    async def fetch_message(self, mid):
        if mid not in self.messages:
            raise discord.NotFound(SimpleNamespace(status=404, reason="nf"), "Unknown Message")
        return self.messages[mid]


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(monthly_board, "STATE_PATH", tmp_path / "board.json")
    channel = FakeChannel()
    tasks = [task("1")]

    async def in_range(first, last):
        return tasks

    async def artist_map():
        return {"a1": Artist(page_id="a1", name="辰葳", discord_user_id="1")}

    monkeypatch.setattr(repositories, "get_tasks_in_range", in_range)
    monkeypatch.setattr(repositories, "get_artist_map", artist_map)
    monkeypatch.setattr(bulletin_cog, "datetime", SimpleNamespace(now=lambda tz=None: NOW))
    cog = SimpleNamespace(bot=SimpleNamespace(get_channel=lambda _: channel))

    def run():
        return asyncio.run(bulletin_cog.BulletinCog.sync_monthly_board(cog))

    return SimpleNamespace(run=run, channel=channel, tasks=tasks)


class TestSync:
    def test_第一次發一則_內容沒變不動_有變原地編輯(self, board):
        assert board.run() == "created"
        assert board.run() == "skipped"
        board.tasks.append(task("2", start=datetime(2026, 10, 20, 19, 0, tzinfo=TZ), end=None))
        assert board.run() == "updated"
        assert board.channel.sent == 1  # 始終是同一則
        assert "任務2" in board.channel.messages[101].content

    def test_訊息被刪掉就重發(self, board):
        board.run()
        board.channel.messages.clear()
        board.tasks.append(task("3"))
        assert board.run() == "created"
        assert board.channel.sent == 2
