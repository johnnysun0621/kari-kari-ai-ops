"""小律記憶（M6-11，Johnny 2026-11-08 定）：每位藝人一份，讓小律叫對稱呼、接得上上次的話。

- 只存在本機 `.state/ritsu_memory.json`（Johnny 定：不存 Notion）。repo 是公開的，`.state/` 不進 git
- 稱呼：藝人說「叫我○○」當下就記；聊天、每日早報、審核結果私訊都用
- 重點：每天半夜讀當天的對話整理成最多 5 行，聊天時帶給小律參考
- 不記：健康、菸酒、感情、家庭、財務、對公司的抱怨。prompt 要求一次，程式關鍵字再擋一次
- 藝人可以問「你記得我什麼」、說「忘掉」；控方用 `/小律記憶` 看或清
- 小律不會自己改自己的規則：每週把答不出來的對話整理給營運，由人決定（`weekly_report_text`）
"""
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from services import ritsu_voice

logger = logging.getLogger(__name__)

STATE_PATH = Path(".state/ritsu_memory.json")
MAX_NOTES = 5
MAX_NOTE_CHARS = 40
MAX_NICKNAME_CHARS = 12
MAX_TRANSCRIPT_CHARS = 4000
SUMMARY_MAX_TOKENS = 300

# 不記的東西：AI 整理時就被要求不寫，這裡是第二道關卡（寧可少記，不要記錯東西）
SENSITIVE = re.compile(
    r"菸|煙|香菸|電子菸|酒|喝醉|藥|病|醫|診|看醫生|憂鬱|焦慮|失眠|受傷|懷孕|減肥|體重"
    r"|感情|戀愛|男友|女友|男朋友|女朋友|分手|曖昧|告白|約會"
    r"|家人|家裡|爸|媽|哥哥|姊姊|姐姐|弟弟|妹妹|爺爺|奶奶"
    r"|錢|債|貸款|薪|酬勞|收入|存款|通告費|合約|抱怨|不爽|討厭公司|離職|不想做了"
)
_NICKNAME_RE = re.compile(r"叫我\s*[「『\"']?([^\s，,。！!？?」』\"'～~]{1,16})")
_NICKNAME_NEGATION = re.compile(r"(不要|別|不用|不准|不能|不可以)\s*$")
_NICKNAME_SUFFIX = re.compile(r"(就好了|就好|就可以|好不好|好嗎|可以嗎|嗎|吧|啦|喔|哦|呀|唷|～|~)+$")
_NOT_A_NAME = re.compile(r"起床|做|去|幫|交|回|來|看|打|唸|念|什麼|甚麼|誰|哪|怎麼")
_RECALL_RE = re.compile(r"(記得|知道|記住)(了)?(我|關於我)(的)?(什麼|甚麼|哪些|多少|些什麼)|你對我(有)?(什麼|甚麼)?(印象|記憶)")
_FORGET_RE = re.compile(
    r"^(小律)?[，,\s]*(請|幫我|你)?(把(我|那些|我說的|你記得的)[^，,。]{0,4})?"
    r"(忘掉|忘記|清掉|刪掉)(我說的|關於我的|我|你記得的|記憶|全部|一切|那些)?"
    r"(吧|啦|喔|好了|好嗎|。|！|!|～|~)*$"
)


def _empty_state() -> dict:
    return {"artists": {}, "meta": {}}


_state: dict | None = None


def reset_for_tests() -> None:
    global _state
    _state = None


def _load() -> dict:
    global _state
    if _state is None:
        try:
            raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            _state = raw if isinstance(raw, dict) else _empty_state()
        except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
            _state = _empty_state()
        _state.setdefault("artists", {})
        _state.setdefault("meta", {})
    return _state


def _save() -> None:
    """先寫暫存檔再換名：寫到一半當掉也不會把整份記憶弄壞。"""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_load(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def get(artist_page_id: str) -> dict:
    return dict(_load()["artists"].get(artist_page_id) or {})


def has_memory(artist_page_id: str) -> bool:
    entry = get(artist_page_id)
    return bool(entry.get("nickname") or entry.get("notes"))


def artists_with_memory() -> list[tuple[str, dict]]:
    """[(藝人 page id, 記憶)]，給 /小律記憶 列選單。"""
    return [(page_id, dict(entry)) for page_id, entry in _load()["artists"].items()
            if entry.get("nickname") or entry.get("notes")]


def call_name_for(artist) -> str:
    """小律怎麼叫這位藝人：有記住的稱呼就用，否則用名字（拿掉尾巴括號）。"""
    nickname = get(artist.page_id).get("nickname")
    return nickname or ritsu_voice.call_name(artist.name)


def _touch(artist, now: datetime | None) -> dict:
    entry = _load()["artists"].setdefault(artist.page_id, {})
    entry["artist_name"] = artist.name
    entry["updated_at"] = (now or datetime.now()).isoformat(timespec="minutes")
    return entry


def set_nickname(artist, nickname: str, now: datetime | None = None) -> None:
    _touch(artist, now)["nickname"] = nickname
    _save()


def clean_notes(notes) -> list[str]:
    """AI 整理出來的重點：去掉敏感的、太長的、重複的，最多 5 行。"""
    if not isinstance(notes, list):
        return []
    cleaned: list[str] = []
    for note in notes:
        if not isinstance(note, str):
            continue
        note = " ".join(note.replace("**", "").split()).strip("・-• ")
        if not note or len(note) > MAX_NOTE_CHARS or SENSITIVE.search(note) or note in cleaned:
            continue
        cleaned.append(note)
    return cleaned[:MAX_NOTES]


def set_notes(artist, notes, now: datetime | None = None) -> None:
    _touch(artist, now)["notes"] = clean_notes(notes)
    _save()


def clear(artist_page_id: str) -> bool:
    removed = _load()["artists"].pop(artist_page_id, None) is not None
    if removed:
        _save()
    return removed


def meta(key: str, default=None):
    return _load()["meta"].get(key, default)


def set_meta(key: str, value) -> None:
    _load()["meta"][key] = value
    _save()


# ---------- 藝人說的話 ----------

def parse_nickname_request(text: str) -> str | None:
    """「小律可以叫我喵喵不要叫我辰葳嗎」→「喵喵」；「不要叫我辰葳」「叫我起床」→ None。"""
    for match in _NICKNAME_RE.finditer(text or ""):
        if _NICKNAME_NEGATION.search(text[:match.start()]):
            continue
        name = _NICKNAME_SUFFIX.sub("", match.group(1))
        name = re.split(r"不要|別|不用", name)[0].strip()
        if not name or len(name) > MAX_NICKNAME_CHARS or _NOT_A_NAME.search(name) or SENSITIVE.search(name):
            continue
        return name
    return None


def is_recall_request(text: str) -> bool:
    return bool(_RECALL_RE.search(text or ""))


def is_forget_request(text: str) -> bool:
    return bool(_FORGET_RE.match((text or "").strip()))


def memory_text(artist_page_id: str) -> str:
    """帶進聊天 prompt 的那段。"""
    entry = get(artist_page_id)
    lines = []
    if entry.get("nickname"):
        lines.append(f"・他希望你叫他：{entry['nickname']}")
    lines += [f"・{note}" for note in entry.get("notes") or []]
    return "\n".join(lines) or "（還沒有記得的事）"


def recall_text(artist_page_id: str, call: str) -> str:
    """藝人問「你記得我什麼」時的回答。"""
    entry = get(artist_page_id)
    lines = []
    if entry.get("nickname"):
        lines.append(f"・叫你{entry['nickname']}")
    lines += [f"・{note}" for note in entry.get("notes") or []]
    if not lines:
        return (
            f"{call}同學，小律目前還沒有記住什麼喔。想換稱呼可以說「叫我○○」，"
            "聊久了小律也會慢慢記得你在練什麼、常問什麼。"
        )
    return "小律記得這些喔～\n" + "\n".join(lines) + "\n\n想要小律忘掉的話，說「忘掉」就好。"


# ---------- 每天半夜整理 ----------

_SUMMARY_RULES = (
    "你是小律，正在整理跟一位藝人同學的聊天筆記，讓之後聊天更貼近他。\n"
    "根據 <old_notes> 和 <today> 的對話，寫出新的筆記，最多 5 行，每行 20 字以內：\n"
    "- 只記對之後聊天有幫助、而且他自己說過的事：正在練的樂器或技巧、喜歡的音樂、常問的事、喜歡怎樣的回答（簡短／詳細）\n"
    "- 絕對不記：健康、菸酒、感情、家庭、金錢、合約酬勞、對公司的抱怨，以及任務名稱、日期、品牌\n"
    "- 不要猜、不要評價他；舊筆記還成立就留著，跟今天說的矛盾就照今天的\n"
    "- 稱呼不用寫（程式另外記）\n"
    "只輸出 JSON：{\"notes\": [\"...\", \"...\"]}"
)


async def summarize(old_notes: list[str], transcript: str, *, state: dict, now: datetime, client=None) -> list[str] | None:
    """把今天的對話整理成新的筆記。AI 用不了回 None（保留舊筆記）。"""
    user = "\n".join([
        "<old_notes>", "\n".join(old_notes) or "（沒有）", "</old_notes>",
        "<today>", transcript[-MAX_TRANSCRIPT_CHARS:], "</today>",
    ])
    parsed = await ritsu_voice._ask_json(
        _SUMMARY_RULES, user, state=state, now=now, client=client, max_tokens=SUMMARY_MAX_TOKENS,
    )
    if not parsed or "notes" not in parsed:
        return None
    return clean_notes(parsed.get("notes"))


# ---------- 每週小律週報 ----------

UNANSWERED_RE = re.compile(
    r"請(你)?(直接)?(問|向|跟|找)公司|查不到|沒有(這方面的)?資料|這裡沒有|不能代表公司|做不到|小律不知道"
    r"|小律可以幫你查自己的任務"  # 聽不懂時的固定說明
)


@dataclass
class ArtistWeek:
    call: str
    questions: int = 0
    unanswered: list[str] = field(default_factory=list)


def unanswered_questions(turns: list[tuple[str, str]]) -> list[str]:
    """小律答不出來（或請他問公司）的那幾題：取那則回答前面藝人說的話。"""
    found = []
    last_question = ""
    for role, text in turns:
        if role == "artist":
            last_question = text
        elif last_question and UNANSWERED_RE.search(text):
            found.append(last_question)
            last_question = ""
    return found


def weekly_report_text(start: datetime, end: datetime, weeks: list[ArtistWeek], feedback_count: int | None) -> str:
    asked = [week for week in weeks if week.questions]
    lines = [f"🤖 **小律週報**（{start.month}/{start.day}～{end.month}/{end.day}）"]
    if not asked:
        lines.append("・這週沒有藝人問小律")
    else:
        total = sum(week.questions for week in asked)
        misses = [(week.call, question) for week in asked for question in week.unanswered]
        lines.append(f"・{len(asked)} 位藝人問了 {total} 次")
        lines.append(f"・答不出來、或請他問公司：{len(misses)} 次")
        for call, question in misses[:10]:
            short = question if len(question) <= 40 else question[:39] + "…"
            lines.append(f"  - {call}：「{short}」")
        if len(misses) > 10:
            lines.append(f"  - …另 {len(misses) - 10} 次")
    if feedback_count is not None:
        lines.append(f"・藝人按「回報問題」：{feedback_count} 次（在 Notion 布告欄的「🙋 使用回饋」）")
    lines.append("要調整小律的回答，跟工程端說；小律不會自己改規則。")
    return "\n".join(lines)
