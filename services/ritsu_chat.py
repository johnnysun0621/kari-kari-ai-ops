"""跟小律聊天（M6-08，Johnny 2026-10-24）：每次對話都像在跟真的小律說話。

- 每則回覆都帶上這位藝人「今天的任務狀況」：所以同一句話，不同藝人、不同天會得到不一樣的回答
- 記得同一個討論串最近幾句（只放在記憶體、不寫檔；隔天或 bot 重開就重新開始），可以接著問「那第二個呢」
- 任務清單照舊由程式列（混合）：小律負責清單上面那段話；清單以外的閒聊也由小律回，最後自然帶回任務
- 事實只能來自 <today_tasks>：沒寫的請藝人問公司，不能編任務、日期、狀態；不能幫忙交件或按收到
- 任何失敗都回 None，呼叫端退回原本的固定回答（不會因為 AI 掛掉而沒回應）
"""
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime

from services import ritsu_voice
from services.notion_links import strip_notion_links

logger = logging.getLogger(__name__)

HISTORY_TURNS = 10  # 記得最近幾則（藝人＋小律各算一則）
MAX_TURN_CHARS = 400
MAX_REPLY_CHARS = 700
MAX_TASKS_IN_CONTEXT = 20
CHAT_MAX_TOKENS = 500
_WEEKDAYS = "一二三四五六日"
_URL = re.compile(r"https?://\S+|www\.\S+")


@dataclass
class Conversation:
    day: date
    turns: deque = field(default_factory=lambda: deque(maxlen=HISTORY_TURNS))

    def add(self, role: str, text: str) -> None:
        self.turns.append((role, strip_notion_links(text)[:MAX_TURN_CHARS]))


class Memory:
    """每位藝人一段對話，只放記憶體。跨日自動清空（小律的「今天」換了）。"""

    def __init__(self):
        self._by_user: dict[str, Conversation] = {}

    def get(self, user_id, today: date) -> Conversation:
        convo = self._by_user.get(str(user_id))
        if convo is None or convo.day != today:
            convo = Conversation(day=today)
            self._by_user[str(user_id)] = convo
        return convo


def today_tasks_text(tasks, now: datetime, deadline_text) -> str:
    """這位藝人今天的任務狀況（只給這位藝人自己的任務）。"""
    if not tasks:
        return "（目前沒有進行中的任務）"
    lines = []
    for task in tasks[:MAX_TASKS_IN_CONTEXT]:
        flags = []
        if task.discord_acknowledged_at is None and task.status in ("尚未開始", None, ""):
            flags.append("還沒按收到")
        line = f"・{strip_notion_links(task.name)}｜{task.task_type or '任務'}｜{deadline_text(task)}｜{task.status or '未填狀態'}"
        lines.append(line + (f"｜{'、'.join(flags)}" if flags else ""))
    if len(tasks) > MAX_TASKS_IN_CONTEXT:
        lines.append(f"（另有 {len(tasks) - MAX_TASKS_IN_CONTEXT} 筆沒列出）")
    return "\n".join(lines)


_RULES = (
    "你現在在 Discord 的私人討論串裡，跟一位藝人同學一對一聊天。\n"
    "- 用小律的口吻，像真的在聊天：先回應對方說的話，再自然地帶到他今天的任務（如果有值得提的）\n"
    "- 任務的事只能根據 <today_tasks>：名稱、日期、狀態照抄，不能編造或推測；資料沒寫的，請他問公司\n"
    "- 小律只能幫忙查，不能幫他交件、按收到或改任務；要做這些請他用 /我的任務\n"
    "- 像真人聊天一樣不要重複：<history> 裡已經提過的任務提醒，這次就不要再講；一次最多提一件最要緊的事\n"
    "- 對方只是打招呼、道謝、閒聊時，簡短回應就好，不用每次都把任務講一遍\n"
    "- 不要網址、不要 Notion 連結、不要 markdown 標題；最多三句，口語、溫暖\n"
    "- 不要叫對方全名，叫他給你的稱呼＋同學\n"
    "只輸出 JSON：{\"reply\": \"...\"}"
)
_LIST_RULES = (
    "\n- 這次程式會在你的話「下面」列出 <list> 這份清單：你的話是清單上面的開場，一到三句，"
    "可以點出最需要先注意的一兩筆（照 <list> 抄名稱和期限），不要把整份清單再列一次\n"
    "- 對方問的範圍只看 <list>：<today_tasks> 裡不在 <list> 的任務，絕對不能說成在這個範圍內"
    "（例如問這週，就不能把下週的任務說成這週的）。<list> 是空的就輕鬆回應，頂多用「另外」提一句範圍外最要緊的事"
)


def _history_text(convo: Conversation) -> str:
    if not convo.turns:
        return "（這是今天第一句）"
    return "\n".join(f"{'藝人' if role == 'artist' else '小律'}：{text}" for role, text in convo.turns)


def _clean_reply(text) -> str | None:
    if not isinstance(text, str):
        return None
    text = _URL.sub("", strip_notion_links(text)).replace("**", "").strip()
    if not text or len(text) > MAX_REPLY_CHARS or "@" in text:
        return None
    return text


async def reply(
    *, call_name: str, question: str, tasks, now: datetime, deadline_text, convo: Conversation,
    state: dict, list_text: str | None = None, client=None,
) -> str | None:
    """小律這一句的回覆。list_text 有給＝清單模式（程式會把清單接在後面）。失敗回 None。"""
    rules = _RULES + (_LIST_RULES if list_text is not None else "")
    user = "\n".join([
        f"今天是 {now.month}/{now.day}（星期{_WEEKDAYS[now.weekday()]}）。對方的稱呼：{call_name}",
        "<today_tasks>", today_tasks_text(tasks, now, deadline_text), "</today_tasks>",
        *(["<list>", list_text, "</list>"] if list_text is not None else []),
        "<history>", _history_text(convo), "</history>",
        f"藝人現在說：{question[:MAX_TURN_CHARS]}",
    ])
    parsed = await ritsu_voice._ask_json(
        ritsu_voice.qa_persona() + "\n\n" + rules, user, state=state, now=now, client=client, max_tokens=CHAT_MAX_TOKENS,
    )
    text = _clean_reply(parsed.get("reply")) if parsed else None
    logger.info("小律聊天（%s，%s token）", "ai" if text else "fallback", (parsed or {}).get("_tokens", 0))
    return text
