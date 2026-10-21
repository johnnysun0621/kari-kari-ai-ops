"""控方通知的統一格式（M3-25 第一階段，2026-10-20）。

「營運內部通知」原本 18 種通知各自的 emoji、粗體、連結都不一樣，一眼分不出哪則要處理。統一成三類：
- 要處理（A）：🟥 要處理｜標題 —— 即時發，附「我來處理／已處理」
- 營運早報（B）：📋 —— 每天一則，見 services/ops_briefing.py
- 系統警告（C）：⚙️ 系統警告｜標題；恢復時同一則改成 🟢 系統恢復
其他（時間池收齊、排課進度等低頻事件）維持原文案。
"""


def action_title(text: str) -> str:
    return f"🟥 **要處理｜{text}**"


def system_title(text: str) -> str:
    return f"⚙️ **系統警告｜{text}**"


def recovered_title(text: str) -> str:
    return f"🟢 **系統恢復｜{text}**"


def link(text: str, url: str | None) -> str:
    """控方訊息裡的 Notion 連結：文字變成可點的連結，<> 讓 Discord 不展開預覽卡片。沒有網址就只回文字。"""
    if not url:
        return text
    safe = text.replace("[", "(").replace("]", ")")
    return f"[{safe}](<{url}>)"


def first_line(content: str) -> str:
    """訊息第一行去掉粗體與類別前綴，給「已處理」收合用。"""
    line = (content or "").strip().splitlines()[0] if (content or "").strip() else ""
    line = line.replace("**", "")
    for prefix in ("🟥 要處理｜", "⚙️ 系統警告｜"):
        if line.startswith(prefix):
            line = line[len(prefix):]
    return line.strip()
