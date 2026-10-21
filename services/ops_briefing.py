"""控方每日營運早報（M3-25 第二階段，2026-10-20）。

「營運內部通知」近 7 天 53 則 bot 訊息、74% 是一筆一則的任務逾期；時間池 6 種通知、每位藝人填完各一則。
改成每天 OPS_BRIEFING_TIME（預設 09:30，排在藝人早報 09:00 之後）一則：
- 🔴 新逾期（逐筆）／仍逾期（只列每位藝人的筆數）
- 🛠 修改期限已過
- 📮 待審核超過 24 小時、沒有可通知的審核人
- 📌 藝人還沒按收到超過 3 天
- 👀 昨天的早報沒看的藝人
- 🎓 老師聯絡不到、或催滿天數仍未結案的課
- 🗓 進行中的時間池進度
- 📣 發文待確認（只在 PUBLISH_CONFIRM_DIGEST_WEEKDAY 那天）
「看某一筆的詳細」與藝人早報一樣展開在同一則，控方版附 Notion 連結；前一天的早報自動刪掉。
即時的「要處理」與系統警告不在這裡（見 services/ops_notice.py）。

這裡是純函式＋.state 讀寫；收集資料與 Discord 在 cogs/ops_briefing.py。
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from services.notion_links import strip_notion_links
from services.ops_notice import link

MESSAGE_LIMIT = 1900
SECTION_LINES = 8  # 每區最多列幾筆，其餘寫「另有 N 筆」
MAX_OPTIONS = 25
UNRECEIVED_DAYS = 3


@dataclass
class OpsLine:
    task_id: str
    text: str
    section: str  # 給選單描述用
    url: str = ""  # Notion 頁面；訊息內文用 linked，選單選項用純文字 text
    who: str = ""
    name: str = ""
    detail: str = ""

    @property
    def linked(self) -> str:
        if not self.url:
            return self.text
        return f"{self.who}｜{link(self.name, self.url)}" + (f"｜{self.detail}" if self.detail else "")


@dataclass
class OpsBriefing:
    new_overdue: list[OpsLine] = field(default_factory=list)
    still_overdue_counts: dict[str, int] = field(default_factory=dict)
    revision_passed: list[OpsLine] = field(default_factory=list)
    review_unassigned: list[OpsLine] = field(default_factory=list)
    unreceived: list[OpsLine] = field(default_factory=list)
    unread_artists: list[str] = field(default_factory=list)
    teacher_notes: list[str] = field(default_factory=list)
    pools: list[str] = field(default_factory=list)
    publish_waiting: list[OpsLine] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any([
            self.new_overdue, self.still_overdue_counts, self.revision_passed, self.review_unassigned,
            self.unreceived, self.unread_artists, self.teacher_notes, self.pools, self.publish_waiting,
        ])

    def option_lines(self) -> list[OpsLine]:
        seen, picked = set(), []
        for line in [*self.new_overdue, *self.revision_passed, *self.review_unassigned, *self.unreceived, *self.publish_waiting]:
            if line.task_id in seen:
                continue
            seen.add(line.task_id)
            picked.append(line)
        return picked[:MAX_OPTIONS]


def briefing_due(now: datetime, last_date: str | None, hour: int, minute: int) -> bool:
    return (now.hour, now.minute) >= (hour, minute) and last_date != now.date().isoformat()


def task_line(
    task_id: str, artist_names: list[str], task_name: str, detail: str, section: str, url: str = "",
) -> OpsLine:
    who = "、".join(artist_names) or "（未關聯藝人）"
    name = strip_notion_links(task_name)
    return OpsLine(
        task_id, f"{who}｜{name}" + (f"｜{detail}" if detail else ""), section,
        url=url or "", who=who, name=name, detail=detail,
    )


def _section(title: str, lines: list[str]) -> list[str]:
    shown = lines[:SECTION_LINES]
    out = ["", f"{title}（{len(lines)}）", *[f"・{line}" for line in shown]]
    if len(lines) > len(shown):
        out.append(f"・…另有 {len(lines) - len(shown)} 筆")
    return out


_LINK = re.compile(r"\[([^\]]*)\]\(<[^>]*>\)")


def briefing_text(day: date, b: OpsBriefing) -> str:
    """每一筆任務都帶 Notion 連結；連結讓訊息變長，超過上限就退回純文字（詳細仍可從選單開）。"""
    text = _briefing_text(day, b)
    if len(text) > MESSAGE_LIMIT:
        text = _LINK.sub(r"\1", text)
    if len(text) > MESSAGE_LIMIT:
        text = text[: MESSAGE_LIMIT - 40].rstrip() + "\n…（太長了，完整清單請用 `/任務總覽`）"
    return text


def _briefing_text(day: date, b: OpsBriefing) -> str:
    weekdays = "一二三四五六日"
    lines = [f"📋 **{day.month}/{day.day}（{weekdays[day.weekday()]}）營運早報**"]
    if b.is_empty():
        lines.append("✅ 今天沒有需要注意的事。")
        return "\n".join(lines)
    if b.new_overdue:
        lines += _section("🔴 **新逾期**", [line.linked for line in b.new_overdue])
    if b.still_overdue_counts:
        total = sum(b.still_overdue_counts.values())
        parts = "、".join(f"{name} {n}" for name, n in sorted(b.still_overdue_counts.items(), key=lambda kv: -kv[1]))
        lines += ["", f"🔴 **仍逾期**（{total}）｜{parts}"]
    if b.revision_passed:
        # M3-25：和其他任務列一致，任務名稱要直接連到 Notion；控方才能從早報立即處理。
        lines += _section("🛠 **修改期限已過**", [line.linked for line in b.revision_passed])
    if b.review_unassigned:
        lines += _section("📮 **待審核超過 24 小時，沒人負責**", [line.linked for line in b.review_unassigned])
    if b.unreceived:
        lines += _section(f"📌 **藝人還沒按收到超過 {UNRECEIVED_DAYS} 天**", [line.linked for line in b.unreceived])
    if b.unread_artists:
        lines += ["", f"👀 **昨天的早報沒看**：{'、'.join(b.unread_artists)}"]
    if b.teacher_notes:
        lines += _section("🎓 **老師的課後作業**", b.teacher_notes)
    if b.pools:
        lines += ["", "🗓 **時間池**", *[f"・{line}" for line in b.pools]]
    if b.publish_waiting:
        lines += _section("📣 **發文待確認發布**（確認發布後改成「已完成發文」）", [line.linked for line in b.publish_waiting])
    if b.option_lines():
        lines.append("\n想看某一筆的詳細，用下方選單（附 Notion 連結）。")
    return "\n".join(lines)


def collapsed_previous(day_code: str) -> str:
    return f"📋 {day_code[4:6]}/{day_code[6:]} 營運早報（已由今天的取代）"


# ---------- .state（notification_log 的共用 state） ----------

def record_new_overdue(state: dict, task_id: str, now: datetime) -> None:
    """截止前提醒 loop 把任務改成已逾期時記下來，改在早報的「新逾期」列，不再一筆一則即時發。"""
    state.setdefault("ops_new_overdue", {})[task_id] = now.isoformat()


def new_overdue_ids(state: dict) -> set[str]:
    return set(state.get("ops_new_overdue", {}))


def add_teacher_note(state: dict, text: str) -> None:
    notes = state.setdefault("ops_teacher_notes", [])
    if text not in notes:
        notes.append(text)


def teacher_notes(state: dict) -> list[str]:
    return list(state.get("ops_teacher_notes", []))


def clear_queues(state: dict) -> None:
    state["ops_new_overdue"] = {}
    state["ops_teacher_notes"] = []


def record_unread(state: dict, day_code: str, artist_name: str) -> None:
    """藝人早報被隔天的取代時，前一天沒按「我都看到了」就記下來，給營運早報的「昨天的早報沒看」。"""
    names = state.setdefault("briefing_unread", {}).setdefault(day_code, [])
    if artist_name and artist_name not in names:
        names.append(artist_name)


def unread_for(state: dict, day_code: str) -> list[str]:
    return list(state.get("briefing_unread", {}).get(day_code, []))


def prune_unread(state: dict, keep_day_codes: set[str]) -> None:
    unread = state.get("briefing_unread", {})
    for code in list(unread):
        if code not in keep_day_codes:
            unread.pop(code)
