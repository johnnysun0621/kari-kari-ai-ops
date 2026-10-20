"""M3-25 第一階段：控方通知只發營運內部通知、統一格式、要處理的可以認領／標已處理。"""
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import config
from cogs import channel_guard, ops_actions
from services import drive_health, ops_notice

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 21, 11, 20, tzinfo=config.TZ)


def test_控方通知不准備援到藝人看得到的頻道():
    """2026-10-20 盤點：營運內部通知沒設定時，逾期／派發失敗／藝人回報會改發到「任務查詢入口」、
    時間池與排課進度會改發到「時間管理」——藝人都看得到。一律改走 channel_guard.ops_channel()。"""
    fallback = re.compile(r"or\s+(?:self\.bot|interaction\.client|client|bot)\.get_channel\(config\.(?:TASK|SCHEDULE)_CHANNEL_ID\)")
    offenders = []
    for path in [*(ROOT / "cogs").glob("*.py"), *(ROOT / "services").glob("*.py"), ROOT / "bot.py"]:
        text = path.read_text(encoding="utf-8")
        if fallback.search(text):
            offenders.append(path.name)
        if path.name not in {"channel_guard.py", "server_setup.py", "scheduling_sla.py"} and re.search(
            r"get_channel\(config\.ADMIN_NOTIFY_CHANNEL_ID\)", text
        ):
            offenders.append(f"{path.name}（直接取營運內部通知，請改用 channel_guard.ops_channel）")
    assert not offenders, offenders


def test_沒設定營運內部通知就不發(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_NOTIFY_CHANNEL_ID", 0)
    client = SimpleNamespace(get_channel=lambda _id: object())

    assert channel_guard.ops_channel(client) is None


def test_要處理與系統警告的格式():
    assert ops_notice.action_title("藝人回報任務問題：A") == "🟥 **要處理｜藝人回報任務問題：A**"
    assert ops_notice.system_title("Drive") == "⚙️ **系統警告｜Drive**"
    assert ops_notice.first_line("🟥 **要處理｜藝人回報任務問題：A**\n藝人：方晨") == "藝人回報任務問題：A"


def test_我來處理_同一則只留一行處理中():
    content = "🟥 **要處理｜新提交待審核**\n任務：week4"
    once = ops_actions.claimed_content(content, "Johnny", NOW)
    twice = ops_actions.claimed_content(once, "小律", NOW)

    assert once.endswith("👤 Johnny 處理中（10/21 11:20）")
    assert twice.count("處理中") == 1 and twice.endswith("👤 小律 處理中（10/21 11:20）")


def test_已處理_收成一行():
    content = "🟥 **要處理｜藝人回報任務問題：拍咒術迴戰影片**\n藝人：方晨\n👤 Johnny 處理中（10/21 11:15）"

    assert ops_actions.done_content(content, "Johnny", NOW) == "✅ 已處理｜藝人回報任務問題：拍咒術迴戰影片｜Johnny 10/21 11:20"


def test_按鈕重啟後仍可用():
    view = ops_actions.ops_action_view()
    assert view.timeout is None and view.is_persistent()
    assert [child.custom_id for child in view.children] == ["ops_claim", "ops_done"]


def test_Drive_失敗與恢復用系統格式():
    assert drive_health.failure_message("授權失效", "x").startswith("⚙️ **系統警告｜Google Drive 健康檢查失敗：授權失效**")
    assert drive_health.recovered_message("10/22").startswith("🟢 **系統恢復｜Google Drive 恢復正常**（10/22 起的問題）")
