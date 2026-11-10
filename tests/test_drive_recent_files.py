"""交件驗證要往子資料夾找（2026-11-10 辰葳：檔案都放在自己分的歌名資料夾裡，小律卻說找不到）。"""
from datetime import datetime

import config
from services import drive_api

FOLDER = drive_api._FOLDER_MIME


class FakeFiles:
    def __init__(self, tree):
        self.tree = tree
        self.queries = []

    def list(self, q, pageToken=None, **kwargs):
        self.queries.append(q)
        parent = q.split("'")[1]
        items = [dict(item) for item in self.tree.get(parent, [])]
        return type("Req", (), {"execute": lambda self_: {"files": items}})()


class FakeService:
    def __init__(self, tree):
        self._files = FakeFiles(tree)

    def files(self):
        return self._files


SINCE = datetime(2026, 11, 9, 20, 29, tzinfo=config.TZ)


def test_子資料夾裡的檔案也找得到_帶路徑():
    tree = {
        "ver1": [{"id": "song", "name": "喵喵高歌咪咪咪", "mimeType": FOLDER}],
        "song": [
            {"id": "s1", "name": "ソラニン", "mimeType": FOLDER},
            {"id": "s2", "name": "菸癮", "mimeType": FOLDER},
        ],
        "s1": [{"id": "f1", "name": "IMG_6786.MOV", "mimeType": "video/quicktime", "size": "138588154"}],
        "s2": [{"id": "f2", "name": "IMG_6750.MOV", "mimeType": "video/quicktime", "size": "172000636"}],
    }
    service = FakeService(tree)
    files = drive_api._list_recent_files_sync(service, "ver1", SINCE)
    assert sorted(f["path"] for f in files) == ["喵喵高歌咪咪咪/ソラニン/IMG_6786.MOV", "喵喵高歌咪咪咪/菸癮/IMG_6750.MOV"]
    assert all("mimeType = 'application/vnd.google-apps.folder'" in q for q in service._files.queries), "子資料夾不看時間，都要往下找"
    assert "2026-11-09T12:19:00Z" in service._files.queries[0], "檔案仍然只算入口建立前 10 分鐘之後的"


def test_最外層的檔案照舊找得到():
    tree = {"ver1": [{"id": "f1", "name": "作業.pdf", "mimeType": "application/pdf"}]}
    files = drive_api._list_recent_files_sync(FakeService(tree), "ver1", SINCE)
    assert [f["path"] for f in files] == ["作業.pdf"]


def test_子資料夾最多往下3層():
    tree = {
        "ver1": [{"id": "d1", "name": "a", "mimeType": FOLDER}],
        "d1": [{"id": "d2", "name": "b", "mimeType": FOLDER}],
        "d2": [{"id": "d3", "name": "c", "mimeType": FOLDER}],
        "d3": [{"id": "d4", "name": "d", "mimeType": FOLDER},
               {"id": "ok", "name": "第三層.mov", "mimeType": "video/quicktime"}],
        "d4": [{"id": "deep", "name": "太深.mov", "mimeType": "video/quicktime"}],
    }
    files = drive_api._list_recent_files_sync(FakeService(tree), "ver1", SINCE)
    assert [f["path"] for f in files] == ["a/b/c/第三層.mov"]
