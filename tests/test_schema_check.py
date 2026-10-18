"""啟動時的 Notion 選項檢查：程式拿來篩選的選項被刪除／改名時要警告控方。"""
import asyncio

from services import notion_api, schema_check


def schema(prop, options, kind="select"):
    return {"properties": {prop: {"type": kind, kind: {"options": [{"name": o} for o in options]}}}}


def test_少了選項會列出缺哪些與影響():
    check = schema_check.OptionCheck("ds", "會議類型", ("課程", "公司例會"), "小律受影響")
    missing = schema_check.missing_options(check, schema("會議類型", ["課程", "A&R 會議"]))

    assert missing == ["公司例會"]
    assert schema_check.warning_lines(check, missing) == ["・`會議類型` 少了選項 公司例會 → 小律受影響"]


def test_欄位不存在也要警告():
    check = schema_check.OptionCheck("ds", "任務狀態", ("進行中",), "總覽讀不到")
    lines = schema_check.warning_lines(check, schema_check.missing_options(check, {"properties": {}}))

    assert lines == ["・`任務狀態` 欄位不存在 → 總覽讀不到"]


def test_status_型別也讀得到選項():
    check = schema_check.OptionCheck("ds", "狀態", ("進行中",), "x")
    assert schema_check.missing_options(check, schema("狀態", ["進行中"], kind="status")) == []


def test_全部對得上就沒有警告(monkeypatch):
    async def fake_retrieve(ds):
        return {"properties": {
            name: {"type": "select", "select": {"options": [{"name": o} for o in opts]}}
            for name, opts in all_options.items()
        }}

    all_options = {}
    for check in schema_check.checks():
        all_options.setdefault(check.property_name, set()).update(check.options)
    monkeypatch.setattr(notion_api, "retrieve_data_source", fake_retrieve)

    assert asyncio.run(schema_check.find_problems()) == []


def test_涵蓋每一個拿選項去篩_Notion_的地方():
    """新增 select filter 時要把選項加進 schema_check.checks()，這裡釘住目前的範圍。"""
    covered = {check.property_name for check in schema_check.checks()}
    assert {"任務狀態", "審核狀態", "身份類型", "結果", "會議類型"} <= covered
