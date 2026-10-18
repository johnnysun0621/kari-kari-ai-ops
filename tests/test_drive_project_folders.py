"""M4-07：交件時 Drive 路徑真的用到任務的關聯專案名稱（cog 層串接）。"""
import asyncio
from datetime import datetime

import pytest

import config
from cogs import submissions
from services import drive_api, repositories

NOW = datetime(2026, 10, 13, 15, 0, tzinfo=config.TZ)
ZONE = "https://drive.google.com/drive/folders/Zone123"


def make_task(project_ids=("p1",), drive_folder_url=""):
    return repositories.TaskItem(
        page_id="t1", name="提供稿件", status="待提交", task_type=None,
        artist_page_ids=["a1"], project_page_ids=list(project_ids),
        drive_folder_url=drive_folder_url,
        end=datetime(2026, 10, 16, tzinfo=config.TZ),
    )


ARTIST = repositories.Artist(page_id="a1", name="方晨", discord_user_id="1", drive_folder_url=ZONE)


@pytest.fixture
def drive(monkeypatch):
    calls = []

    async def ensure_folder_path(*, base_folder_id, path_segments):
        calls.append((base_folder_id, list(path_segments)))
        return {"id": "F1", "webViewLink": "https://drive.google.com/drive/folders/F1"}

    async def set_task_drive_folder(page_id, url):
        return None

    async def project_map():
        return {"p1": repositories.ProjectItem(page_id="p1", name="方晨＿哈根達斯雪酥限定",
                                                status="進行中", project_type="藝人外部商業合作")}

    monkeypatch.setattr(drive_api, "ensure_folder_path", ensure_folder_path)
    monkeypatch.setattr(repositories, "set_task_drive_folder", set_task_drive_folder)
    monkeypatch.setattr(repositories, "get_project_map", project_map)
    return calls


def run(task):
    return asyncio.run(submissions._resolve_submission_target(task, ARTIST, NOW))


def test_交件資料夾在藝人專區的專案夾底下(drive):
    target, url = run(make_task())
    assert drive == [("Zone123", ["方晨＿哈根達斯雪酥限定", "2026.10.16_提供稿件"])]
    assert url.endswith("/F1")


def test_沒掛專案進未分專案(drive):
    run(make_task(project_ids=()))
    assert drive[0][1][0] == "未分專案"


def test_已回寫任務夾就直接用_不再查專案(drive, monkeypatch):
    async def boom(task):
        raise AssertionError("不該再查專案")

    monkeypatch.setattr(repositories, "task_project_name", boom)
    target, _ = run(make_task(drive_folder_url="https://drive.google.com/drive/folders/TaskF"))
    assert target == ("TaskF", [])
    assert drive == []


def test_讀不到專案時不建資料夾_不會放錯到未分專案(drive, monkeypatch):
    async def fail(task):
        raise RuntimeError("Notion down")

    monkeypatch.setattr(repositories, "task_project_name", fail)
    assert run(make_task()) == (None, "")
    assert drive == []
