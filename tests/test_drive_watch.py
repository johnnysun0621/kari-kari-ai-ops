"""M4-23：藝人直接傳到 Drive 的作業——找出來、私訊確認、補登交件、營運早報。"""
import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import config
from cogs import drive_watch, submissions
from services import audit, drive_api, ops_briefing, repositories, stray_uploads

TZ = config.TZ
FOLDER = stray_uploads.FOLDER_MIME
NOW_UTC = datetime(2026, 11, 11, 13, 0, tzinfo=timezone.utc)  # 台灣 21:00


def item(id_, name, folder=False, mine=False, created="2026-11-09T10:00:00Z", size="100"):
    return {"id": id_, "name": name, "mimeType": FOLDER if folder else "video/quicktime", "size": size,
            "createdTime": created, "modifiedTime": created, "owners": [{"me": mine}]}


class FakeDrive:
    def __init__(self, tree):
        self.tree = tree

    def files(self):
        tree = self.tree

        class Files:
            def list(self, q, **kw):
                parent = q.split("'")[1]
                return SimpleNamespace(execute=lambda: {"files": tree.get(parent, [])})

        return Files()


# 方晨 11/9 的實際樣子：小律建的專區裡，她自己建了「週練習week6」
TREE = {
    "zone": [
        item("proj", "方晨 2611 週練習", folder=True, mine=True),
        item("week6", "週練習week6", folder=True),
        item("loose1", "隨手丟的.mov", created="2026-11-10T02:00:00Z"),
        item("ritsu_file", "小律自己的檔.pdf", mine=True),
    ],
    "proj": [item("task", "2026.11.09_11/8 week2", folder=True, mine=True)],
    "task": [item("ver1", "11/8 week2_2026.11.08_ver.1", folder=True, mine=True),
             item("fb", "老師回饋", folder=True, mine=True)],
    "ver1": [item("in_ver", "正常交件.mov")],
    "fb": [item("fb1", "批改.pdf")],
    "week6": [item("listen", "聽力", folder=True), item("run", "跑步1.jpg"),
              item("sing", "唱歌.MOV", created="2026-11-09T11:29:00Z", size="1048576")],
    "listen": [item(f"p{i}", f"IMG_{i}.PNG") for i in range(15)],
}


def test_找出不在交件資料夾裡的檔案_依資料夾分組():
    groups = {g.key: g for g in stray_uploads.scan_zone_sync(FakeDrive(TREE), "zone", "方晨")}
    assert set(groups) == {"fweek6", "lzone"}, "交件資料夾、老師回饋、小律自己的檔案都不算"
    week6 = groups["fweek6"]
    assert week6.kind == "folder" and week6.count == 17 and week6.name == "週練習week6"
    assert week6.last_uploaded == "2026-11-09T11:29:00Z" and week6.label() == "「週練習week6」"
    loose = groups["lzone"]
    assert loose.kind == "loose" and loose.file_ids == ["loose1"] and loose.label() == "直接放在「方晨」的檔案"


def group(last="2026-11-09T11:29:00Z", key="fweek6"):
    return stray_uploads.StrayGroup(key=key, kind="folder", folder_id="week6", name="週練習week6", location="",
                                    file_ids=["x"], count=17, total_bytes=1, last_uploaded=last)


def test_還在上傳或太舊的不問_處理過的不重問():
    assert stray_uploads.needs_asking(group(), NOW_UTC)
    assert not stray_uploads.needs_asking(group(last="2026-11-11T12:45:00Z"), NOW_UTC), "最後一個檔案不到 30 分鐘"
    assert not stray_uploads.needs_asking(group(last="2026-10-20T00:00:00Z"), NOW_UTC), "超過 14 天"
    stray_uploads.update_group("fweek6", status="asked")
    assert not stray_uploads.needs_asking(group(), NOW_UTC), "已經問過、還沒回"
    stray_uploads.update_group("fweek6", status="ignored", handled_at="2026-11-10T00:00:00+00:00")
    assert not stray_uploads.needs_asking(group(), NOW_UTC), "說過不是作業"
    assert stray_uploads.needs_asking(group(last="2026-11-11T05:00:00Z"), NOW_UTC), "之後又傳了新檔案就再問"


def task(page_id, name, end, status="進行中"):
    return repositories.TaskItem(page_id=page_id, name=name, status=status, task_type=None,
                                 artist_page_ids=["a1"], open_for_submission=True, end=end)


def test_推薦順序_關鍵字優先_再看期限():
    tasks = [
        task("y2k", "11/10（二）極限Y2K穿搭轉場影片", datetime(2026, 11, 10, tzinfo=TZ)),
        task("w3", "11/15（日）week3 聽力", datetime(2026, 11, 16, tzinfo=TZ)),
        task("w2", "11/8（日）week2 聽力", datetime(2026, 11, 9, tzinfo=TZ), status="已逾期"),
        task("done", "已經交過的 week1", datetime(2026, 11, 1, tzinfo=TZ), status="待審核"),
    ]
    ordered = [t.page_id for t in stray_uploads.suggest_tasks(tasks, "2026-11-09T11:29:00Z", hint="週練習week6")]
    assert ordered == ["w2", "w3", "y2k"], "待審核的不能再交"


def test_一天兩輪():
    assert stray_uploads.scan_slot(datetime(2026, 11, 11, 11, 59, tzinfo=TZ)) is None
    assert stray_uploads.scan_slot(datetime(2026, 11, 11, 12, 0, tzinfo=TZ)) == "2026-11-11@12"
    stray_uploads.set_meta("last_slot", "2026-11-11@12")
    assert stray_uploads.scan_slot(datetime(2026, 11, 11, 18, 0, tzinfo=TZ)) is None
    assert stray_uploads.scan_slot(datetime(2026, 11, 11, 21, 5, tzinfo=TZ)) == "2026-11-11@21"


ARTIST = repositories.Artist(page_id="a1", name="方晨", discord_user_id="11",
                             drive_folder_url="https://drive.google.com/drive/folders/zone")


@pytest.fixture
def env(monkeypatch):
    calls = SimpleNamespace(moved=[], recorded=[], audits=[], dms=[])

    async def find_artist(uid):
        return ARTIST if str(uid) == "11" else None

    async def get_task(task_id):
        return task("w2", "11/8（日）week2 聽力", datetime(2026, 11, 9, tzinfo=TZ), status="已逾期")

    async def resolve(t, a, now):
        return ("zone", ["方晨 2611 週練習", "2026.11.09_11/8 week2"]), ""

    async def next_version(**kw):
        return {"id": "ver9", "name": "11/8 week2_2026.11.11_ver.1", "webViewLink": "https://drive/ver9"}, ["…"]

    async def move(item_id, parent):
        calls.moved.append((item_id, parent))

    async def list_files(folder_id, since):
        return [{"id": "f1", "name": "唱歌.MOV", "path": "週練習week6/唱歌.MOV", "size": "1048576"}]

    async def record(client, t, a, files, folder_url, **kw):
        calls.recorded.append({"task": t.page_id, "folder_url": folder_url, **kw})
        return {"url": "https://notion/sub", "names": "週練習week6/唱歌.MOV", "count": len(files), "total_mb": 1}

    async def log_event(**kw):
        calls.audits.append(kw)

    monkeypatch.setattr(repositories, "find_artist_by_discord_id", find_artist)
    monkeypatch.setattr(repositories, "get_task", get_task)
    monkeypatch.setattr(submissions, "_resolve_submission_target", resolve)
    monkeypatch.setattr(submissions, "_ensure_next_version_folder", next_version)
    monkeypatch.setattr(submissions, "record_folder_submission", record)
    monkeypatch.setattr(drive_api, "move_item", move)
    monkeypatch.setattr(drive_api, "list_recent_files", list_files)
    monkeypatch.setattr(audit, "log_event", log_event)
    stray_uploads.update_group(
        "fweek6", status="asked", discord_user_id="11", artist_page_id="a1", artist_name="方晨", kind="folder",
        folder_id="week6", name="週練習week6", label="「週練習week6」", location="", file_ids=["x"], count=17,
        last_uploaded="2026-11-09T11:29:00Z",
    )
    return calls


def test_補登_搬進標準資料夾_提交時間用實際上傳時間(env):
    text, done = asyncio.run(drive_watch.adopt(None, 11, "fweek6", "w2"))
    assert done and "已經幫你交到「11/8（日）week2 聽力」" in text and "不用" not in text.split("\n")[0]
    assert env.moved == [("week6", "ver9")], "整個資料夾搬進新的 ver 資料夾"
    rec = env.recorded[0]
    assert rec["folder_url"] == "https://drive/ver9"
    assert rec["submitted_at"] == datetime(2026, 11, 9, 19, 29, tzinfo=TZ), "提交時間＝藝人實際上傳時間"
    assert "原本放在：週練習week6" in rec["note"] and rec["audit_title"] == "提交任務（Drive 補登）"
    entry = stray_uploads.get_group("fweek6")
    assert entry["status"] == "submitted" and entry["task_name"] == "11/8（日）week2 聽力"


def test_搬不動_改原地登記並註明(env, monkeypatch):
    async def fail(item_id, parent):
        raise RuntimeError("insufficientFilePermissions")

    monkeypatch.setattr(drive_api, "move_item", fail)
    text, done = asyncio.run(drive_watch.adopt(None, 11, "fweek6", "w2"))
    assert done and "留在原本的位置" in text
    rec = env.recorded[0]
    assert rec["folder_url"] == "https://drive.google.com/drive/folders/week6"
    assert "搬到標準資料夾失敗" in rec["note"]


def test_別人不能幫你交_交過的不重交(env):
    text, done = asyncio.run(drive_watch.adopt(None, 99, "fweek6", "w2"))
    assert not done and "不是你的檔案" in text and env.recorded == []
    stray_uploads.update_group("fweek6", status="submitted")
    text, done = asyncio.run(drive_watch.adopt(None, 11, "fweek6", "w2"))
    assert done and "已經交過" in text and env.recorded == []


def test_Notion寫入失敗_不標成已交(env, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("Notion 500")

    monkeypatch.setattr(submissions, "record_folder_submission", boom)
    text, done = asyncio.run(drive_watch.adopt(None, 11, "fweek6", "w2"))
    assert not done and "Notion" in text
    assert stray_uploads.get_group("fweek6")["status"] == "asked"


def test_不是作業():
    stray_uploads.update_group("fweek6", status="asked", discord_user_id="11")
    log = []

    async def edit_message(**kw):
        log.append(kw)

    interaction = SimpleNamespace(user=SimpleNamespace(id=11), response=SimpleNamespace(edit_message=edit_message))
    asyncio.run(drive_watch.StrayIgnoreButton("fweek6").callback(interaction))
    assert stray_uploads.get_group("fweek6")["status"] == "ignored" and log[0]["view"] is None


def test_選單和按鈕_bot重開後還原得回來():
    view = drive_watch.stray_view("fweek6", [task("w2", "11/8 week2", datetime(2026, 11, 9, tzinfo=TZ))],
                                  datetime(2026, 11, 11, tzinfo=TZ))
    assert view.is_persistent()
    select, button = view.children
    assert select.custom_id == "stray_pick:fweek6" and button.custom_id == "stray_ignore:fweek6"
    assert drive_watch.StrayTaskSelect.__discord_ui_compiled_template__.fullmatch(select.custom_id)
    long_key = "f" + "1" * 33
    assert len(drive_watch.StrayTaskSelect(long_key).custom_id) <= 100


def test_掃描_私訊藝人並記錄(monkeypatch):
    sent = []

    class User:
        async def send(self, content, view=None):
            sent.append((content, view))

    async def fetch_user(uid):
        return User()

    async def artists():
        return [ARTIST]

    async def tasks_for(page_id):
        return [task("w2", "11/8（日）week2 聽力", datetime(2026, 11, 9, tzinfo=TZ), status="已逾期")]

    async def log_event(**kw):
        pass

    monkeypatch.setattr(repositories, "get_active_artists", artists)
    monkeypatch.setattr(repositories, "get_discord_tasks_for_artist", tasks_for)
    monkeypatch.setattr(drive_api, "_get_service", lambda: FakeDrive(TREE))
    monkeypatch.setattr(audit, "log_event", log_event)
    cog = drive_watch.DriveWatchCog.__new__(drive_watch.DriveWatchCog)
    cog.bot = SimpleNamespace(fetch_user=fetch_user)
    asked = asyncio.run(cog.run_scan(datetime(2026, 11, 11, 21, 0, tzinfo=TZ)))
    assert asked == 2 and len(sent) == 2
    content, view = next(s for s in sent if "週練習week6" in s[0])
    assert "17 個檔案，11/9 上傳" in content and "不用重傳" in content
    assert view.children[0].options[0].value == "w2"
    assert stray_uploads.get_group("fweek6")["status"] == "asked"
    asked_again = asyncio.run(cog.run_scan(datetime(2026, 11, 12, 12, 0, tzinfo=TZ)))
    assert asked_again == 0, "問過的不再問"


def test_掃描_資料夾不見了就不再列(monkeypatch):
    stray_uploads.update_group("fgone", status="asked", artist_page_id="a1")
    stray_uploads.forget_missing("a1", {"fweek6"})
    assert stray_uploads.get_group("fgone")["status"] == "gone"


def test_營運早報_列出還沒處理的_已補登列一次():
    stray_uploads.update_group("fweek6", status="asked", artist_name="方晨", label="「週練習week6」", count=17,
                               last_uploaded="2026-11-09T11:29:00Z")
    stray_uploads.update_group("fdone", status="submitted", artist_name="辰葳", label="「歌」", task_name="week3")
    lines = stray_uploads.ops_lines(TZ)
    assert "方晨｜「週練習week6」（17 個，11/9 上傳）｜已私訊，還沒回" in lines
    assert "已補登：辰葳｜「歌」 → week3" in lines
    assert all("辰葳" not in line for line in stray_uploads.ops_lines(TZ)), "已補登只列一次"
    text = ops_briefing.briefing_text(date(2026, 11, 12), ops_briefing.OpsBriefing(drive_strays=lines))
    assert "📂 **直接傳到 Drive、還沒登記交件**（2）" in text
