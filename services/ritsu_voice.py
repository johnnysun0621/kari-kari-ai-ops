"""小律每天依藝人狀況寫的開頭和結尾一句（M3-30，Johnny 2026-10-23）。

小律取自《暗殺教室》住在教室裡的 AI 同學。早報的事實（任務、筆數、日期、按鈕）一律由程式寫；
這裡只請 AI 寫兩句語氣：開頭一句（接在「早安」後面）、結尾一句（按「我看完了」後）。

- 只送狀況統計，不送藝人名字、不送任務名稱（可能有業配品牌）；名字由程式補
- 人設在 prompts/ritsu_persona.md，每次都重新讀：Johnny 改了隔天就生效
- 沒有 API key、超過每日 token 上限、逾時、回了怪東西 → 改用固定範本句，早報照常準時發
- 每天自己的 token 上限（RITSU_VOICE_DAILY_TOKEN_LIMIT），跟小律問答的額度分開
"""
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

import config
from services import llm_response

logger = logging.getLogger(__name__)

PERSONA_PATH = Path(__file__).resolve().parent.parent / "prompts" / "ritsu_persona.md"
TIMEOUT_SECONDS = 15
MAX_OPENING = 45
MAX_CLOSING = 30
MAX_COMPLETION_TOKENS = 200
DEFAULT_CLOSING = "今天也一起加油！"
_WEEKDAYS = "一二三四五六日"
_BAD = re.compile(r"https?://|www\.|discord\.gg|notion\.|@|`|<|>")


@dataclass
class Counts:
    unreceived: int = 0
    overdue: int = 0
    needs_fix: int = 0
    awaiting: int = 0
    due_soon: int = 0
    yesterday_read: bool | None = None  # None＝昨天沒有早報


@dataclass
class Voice:
    opening: str
    closing: str = DEFAULT_CLOSING
    source: str = "template"
    tokens: int = field(default=0, repr=False)


def daily_limit() -> int:
    try:
        return int(os.getenv("RITSU_VOICE_DAILY_TOKEN_LIMIT", "30000"))
    except ValueError:
        return 30000


def call_name(name: str) -> str:
    """「Johnny (測試）」→「Johnny」：拿掉尾巴括號，接「同學」才自然。"""
    trimmed = re.sub(r"\s*[（(][^）)]*[）)]\s*$", "", name or "").strip()
    return trimmed or (name or "").strip()


def template_voice(counts: Counts) -> Voice:
    """AI 用不了時的固定範本句；也是 AI 寫壞時的後備。"""
    if counts.overdue:
        opening = f"有 {counts.overdue} 筆已經過期限了，小律陪你一起把它們解決吧。"
        closing = "加油，小律一直都在！"
    elif counts.needs_fix:
        opening = f"有 {counts.needs_fix} 筆要修改，照回饋一步一步調整就好。"
        closing = DEFAULT_CLOSING
    elif counts.unreceived:
        opening = "有任務在等你按收到，先看看內容合不合適吧～"
        closing = DEFAULT_CLOSING
    elif counts.due_soon:
        opening = f"有 {counts.due_soon} 筆快到期了，照計畫來，小律幫你盯著。"
        closing = DEFAULT_CLOSING
    else:
        opening = "今天也請多指教！"
        closing = DEFAULT_CLOSING
    return Voice(opening=opening, closing=closing)


def status_text(counts: Counts, now: datetime) -> str:
    lines = [f"今天是星期{_WEEKDAYS[now.weekday()]}。這位藝人今天的狀況："]
    lines.append(f"- 還沒按收到的任務：{counts.unreceived} 筆")
    lines.append(f"- 已經過期限：{counts.overdue} 筆")
    lines.append(f"- 需要修改：{counts.needs_fix} 筆")
    lines.append(f"- 已過截止、等公司確認：{counts.awaiting} 筆")
    lines.append(f"- 快到期：{counts.due_soon} 筆")
    if counts.yesterday_read is True:
        lines.append("- 昨天的早報有看完")
    elif counts.yesterday_read is False:
        lines.append("- 昨天的早報沒有看")
    return "\n".join(lines)


_RULES = (
    "請依照上面的人設，用繁體中文寫兩句話，只輸出 JSON：{\"opening\": \"...\", \"closing\": \"...\"}。\n"
    f"- opening：接在「早安！我是小律。」後面的一句話，最多 {MAX_OPENING - 5} 個字\n"
    f"- closing：藝人看完早報後的一句話，最多 {MAX_CLOSING - 5} 個字\n"
    "- 不要稱呼名字（程式會自己加），不要提具體日期或任務名稱，不要編造狀況裡沒有的事\n"
    "- 不要網址、不要 @、不要 markdown"
)


def _persona() -> str:
    try:
        return PERSONA_PATH.read_text(encoding="utf-8")
    except OSError:
        logger.warning("讀不到小律的人設檔：%s", PERSONA_PATH)
        return "你是小律，一位有禮貌、開朗、很想幫上大家忙的 AI 同學。"


def _clean(text, limit: int) -> str | None:
    if not isinstance(text, str):
        return None
    text = " ".join(text.replace("**", "").split()).strip("「」\"' ")
    if not text or _BAD.search(text):
        return None
    if len(text) > limit:
        return None  # 太長就整句不要，不硬切（切一半的句子比範本句還糟）
    return text


def _used_today(state: dict, today: str) -> int:
    entry = state.get("ritsu_voice") or {}
    return int(entry.get("tokens", 0)) if entry.get("day") == today else 0


def _add_usage(state: dict, today: str, tokens: int) -> None:
    state["ritsu_voice"] = {"day": today, "tokens": _used_today(state, today) + tokens}


async def _ask_json(system_extra: str, user_text: str, *, state: dict, now: datetime,
                    client: httpx.AsyncClient | None = None) -> dict | None:
    """送一次 chat completion，要求回 JSON。沒有 key、額度用完、任何失敗都回 None（呼叫端用範本句）。"""
    tenant = config.get_tenant()
    today = now.date().isoformat()
    if not tenant.llm_api_key:
        return None
    limit = daily_limit()
    if limit > 0 and _used_today(state, today) >= limit:
        logger.info("小律的語氣今天的 token 額度用完，改用範本句")
        return None
    payload = {
        "model": tenant.llm_model,
        "messages": [
            {"role": "system", "content": _persona() + "\n\n" + system_extra},
            {"role": "user", "content": user_text},
        ],
        "response_format": {"type": "json_object"},
    }
    try:
        llm_response.configure_chat_completion(
            payload, base_url=tenant.llm_base_url, model=tenant.llm_model, max_tokens=MAX_COMPLETION_TOKENS,
        )
        owns = client is None
        client = client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        try:
            response = await client.post(
                f"{tenant.llm_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {tenant.llm_api_key}"},
                json=payload,
            )
        finally:
            if owns:
                await client.aclose()
        response.raise_for_status()
        data = response.json()
        tokens = int((data.get("usage") or {}).get("total_tokens") or 0)
        _add_usage(state, today, tokens)
        parsed = json.loads(llm_response.extract_text(data) or "{}")
    except Exception as exc:  # 網路、HTTP、JSON、供應商設定：一律後備，訊息不能因為這個發不出去
        logger.warning("小律的語氣生成失敗，改用範本句：%s", exc)
        return None
    if not isinstance(parsed, dict):
        return None
    parsed["_tokens"] = tokens
    return parsed


async def daily_voice(counts: Counts, *, state: dict, now: datetime, client: httpx.AsyncClient | None = None) -> Voice:
    """這位藝人今天的開頭和結尾。任何失敗都回固定範本句，不丟例外。"""
    fallback = template_voice(counts)
    parsed = await _ask_json(_RULES, status_text(counts, now), state=state, now=now, client=client)
    if parsed is None:
        return fallback
    tokens = parsed.get("_tokens", 0)
    opening = _clean(parsed.get("opening"), MAX_OPENING)
    closing = _clean(parsed.get("closing"), MAX_CLOSING)
    voice = Voice(opening=opening or fallback.opening, closing=closing or fallback.closing,
                  source="ai" if opening and closing else "partial" if opening or closing else "template",
                  tokens=tokens)
    logger.info("小律問候（%s，%s token）：%s｜%s", voice.source, tokens, voice.opening, voice.closing)
    return voice


# ---------- 審核結果（M4-16） ----------

MAX_REVIEW_LINE = 45


@dataclass
class ReviewFacts:
    result: str  # 通過／需修改／退回
    attempt: int = 1  # 這筆任務第幾次交
    pass_streak: int = 0  # 最近連續一次就通過的任務數（含這次）
    recent_passed: int = 0  # 最近 60 天通過幾件
    recent_fixed: int = 0  # 最近 60 天被要求修改／退回幾次


def review_template(facts: ReviewFacts) -> str:
    if facts.result == "通過":
        if facts.pass_streak >= 2:
            return f"連續 {facts.pass_streak} 次一次就過，小律好佩服！"
        if facts.attempt >= 2:
            return "改了之後順利過關，辛苦了！"
        return "辛苦了，這份做得很好！"
    if facts.result == "需修改":
        if facts.attempt >= 2:
            return "再調整一下就好，小律陪你一起改。"
        return "別擔心，照著回饋調整就好，小律陪你。"
    return "這次先退回沒關係，看看回饋，小律陪你重新來過。"


def review_text(facts: ReviewFacts) -> str:
    return "\n".join([
        f"審核結果：{facts.result}",
        f"- 這是這筆任務第 {facts.attempt} 次交件",
        f"- 最近連續一次就通過：{facts.pass_streak} 件",
        f"- 最近 60 天通過 {facts.recent_passed} 件、被要求修改或退回 {facts.recent_fixed} 次",
    ])


_REVIEW_RULES = (
    "藝人剛收到審核結果。請依照上面的人設，用繁體中文寫一句話，只輸出 JSON：{\"line\": \"...\"}。\n"
    f"- 最多 {MAX_REVIEW_LINE - 5} 個字\n"
    "- 通過：真心稱讚；需修改或退回：不責備，給陪伴和打氣\n"
    "- 不要稱呼名字、不要提任務名稱或回饋內容、不要編造狀況裡沒有的事\n"
    "- 不要網址、不要 @、不要 markdown"
)


async def review_line(facts: ReviewFacts, *, state: dict, now: datetime, client: httpx.AsyncClient | None = None) -> str:
    """審核結果通知上小律的一句。只送數字，不送名字、任務名稱、回饋內容。失敗回範本句。"""
    fallback = review_template(facts)
    parsed = await _ask_json(_REVIEW_RULES, review_text(facts), state=state, now=now, client=client)
    line = _clean(parsed.get("line"), MAX_REVIEW_LINE) if parsed else None
    logger.info("小律審核結果（%s）：%s", "ai" if line else "template", line or fallback)
    return line or fallback
