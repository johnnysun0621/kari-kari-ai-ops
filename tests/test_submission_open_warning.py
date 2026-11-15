"""M4-24：要交的作業卻沒開放提交——營運早報預警、/我的任務 標示、收合訊息講清楚「確認收到不等於交了」。

2026-11-15：「編曲仿作-蔡依林」拆成一人一筆時，SOMA、采奕、辰葳三筆漏勾「是否開放提交」，
辰葳看到舊卡片「✅ 已收到」以為交過了，要交時又找不到按鈕。
"""
import asyncio
import dataclasses
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

import config
from cogs import ops_briefing as ops_cog
from cogs import tasks_artist
from services import notification_log, ops_briefing, repositories, task_bundle

TZ = config.TZ
NOW = datetime(2026, 11, 12, 9, 30, tzinfo=TZ)


def task(page_id, name="11/15（日）編曲課作業 編曲仿作-蔡依林", *, project=("課程培訓",), deadline=datetime(2026, 11, 15, tzinfo=TZ),
         open_=False, status="進行中", visible=True):
    return repositories.TaskItem(
        page_id=page_id, name=name, status=status, task_type=None, artist_page_ids=["a1"],
        discord_visible=visible, open_for_submission=open_, project_types=list(project),
        submission_deadline=deadline, end=deadline, url=f"https://www.notion.so/{page_id}",
    )


@pytest.mark.parametrize("t,expected", [
    (task("course"), True),
    (task("weekly", "11/15 week3 聽力", project=("週練習",)), True),
    (task("named", "11/24（二）上傳舞蹈作業", project=("藝人外部商業合作",)), True),
    (task("biz", "11/18 UNOVE 腳本定案", project=("藝人外部商業合作",)), False),
    (task("nodeadline", deadline=None), False),
    (task("opened", open_=True), False),
    (task("submitted", status="待審核"), False),
    (task("done", status="已完成"), False),
    (task("overdue", status="已逾期"), True),
])
def test_哪些算要交作業卻沒開放(t, expected):
    assert repositories.awaiting_submission_open(t) is expected


@pytest.fixture
def repos(monkeypatch, tmp_path):
    monkeypatch.setattr(notification_log, "STATE_PATH", tmp_path / "log.json")
    notification_log.reset_for_tests()
    monkeypatch.setattr(config, "TENANT", dataclasses.replace(config.TENANT, ops_briefing_enabled=True))
    data = SimpleNamespace(tasks=[])

    async def get_active_tasks():
        return data.tasks

    async def get_artist_map():
        return {"a1": repositories.Artist(page_id="a1", name="辰葳", discord_user_id="1")}

    async def empty(*a, **kw):
        return []

    monkeypatch.setattr(repositories, "get_active_tasks", get_active_tasks)
    monkeypatch.setattr(repositories, "get_artist_map", get_artist_map)
    monkeypatch.setattr(repositories, "get_submissions_by_review_status", empty)
    monkeypatch.setattr(repositories, "get_people_by_ids", empty)
    monkeypatch.setattr(repositories, "get_tasks_by_status", empty)
    monkeypatch.setattr(ops_cog.time_collection, "active_collections", lambda: [])
    yield data
    notification_log.reset_for_tests()


def test_營運早報_提交期限3天內才列_已經過了也列(repos):
    repos.tasks = [
        task("soon"),  # 11/15，3 天內
        task("later", deadline=datetime(2026, 11, 22, tzinfo=TZ)),  # 太遠
        task("passed", deadline=datetime(2026, 11, 10, tzinfo=TZ), status="已逾期"),  # 過了還沒開
        task("opened", open_=True),
        task("hidden", visible=False),  # 還沒派發的草稿不列
        task("biz", "11/13 UNOVE 腳本", project=("藝人外部商業合作",), deadline=datetime(2026, 11, 13, tzinfo=TZ)),
    ]
    b = asyncio.run(ops_cog.collect(NOW, notification_log.shared()))
    assert [line.task_id for line in b.submission_closed] == ["soon", "passed"]
    assert b.submission_closed[0].detail == "提交期限 11/15"
    text = ops_briefing.briefing_text(date(2026, 11, 12), b)
    assert "📤 **快到交件期限，還沒開放提交**（2）" in text
    assert ops_briefing.SUBMISSION_CLOSED_HINT in text
    assert text.index("📤") < text.index("🔴"), "最急的放最上面"
    assert b.option_lines()[0].task_id == "soon", "選單也看得到"


def test_我的任務_標出還沒開放交件():
    sections = tasks_artist.task_bucket_sections({"open": [task("t1"), task("t2", open_=True)]}, NOW)
    lines = sections[0][1]
    assert lines[0].endswith(tasks_artist.NOT_OPEN_MARK)
    assert tasks_artist.NOT_OPEN_MARK not in lines[1]


def test_收合訊息_寫清楚是確認收到任務_作業要另外交():
    line = tasks_artist.received_line(task("t1"), datetime(2026, 10, 21, 21, 52, tzinfo=TZ))
    assert line.startswith("✅ 已確認收到任務｜11/15（日）編曲課作業")
    assert "作業還沒交" in line and "/我的任務" in line
    biz = tasks_artist.received_line(task("b", "11/18 UNOVE 腳本", project=("藝人外部商業合作",)), NOW)
    assert "作業還沒交" not in biz, "不用交作業的不要多一句"
    assert "已確認收到任務" in notification_log.received_suffix(NOW)


def test_合併訊息全部收到_有作業的提醒另外交():
    text = task_bundle.done_text([task("t1")], [], NOW, lambda t: "交件 11/15")
    assert "已確認收到 1 筆任務" in text and "記得另外交" in text
    biz = task_bundle.done_text([task("b", "UNOVE", project=("藝人外部商業合作",))], [], NOW, lambda t: "")
    assert "記得另外交" not in biz
