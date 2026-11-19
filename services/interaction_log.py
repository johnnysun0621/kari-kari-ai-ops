"""bot 收到的每一次點擊／送出記一行 log（M3-32，2026-11-19）。

起因：方晨在日本按早報的「這筆不行」，送出時 Discord 顯示「互動失敗」。log 裡完全沒有她那一下，
只能靠「同一分鐘子喬按都正常」排除法推出是她手機連線斷了，查了半小時。
有這一行之後：log 有＝bot 收到了（再看後面有沒有錯）；log 沒有＝根本沒送到 bot（手機或網路那端）。

只記誰、按了什麼、在哪裡；**不記填寫內容**（原因、回饋、時段可能有私人資訊）。自動完成（打字時的建議）太吵，不記。
"""
import logging

import discord

logger = logging.getLogger("karikari.interaction")

_COMPONENT_KINDS = {2: "按鈕", 3: "選單", 5: "選單", 6: "選單", 7: "選單", 8: "選單"}
_MAX_VALUES_SHOWN = 3


def describe(interaction, modal_name: str = "") -> str | None:
    """組出一行描述；不用記的（自動完成、ping）回 None。純函式：只讀 interaction 上的屬性。"""
    kind = getattr(interaction, "type", None)
    data = getattr(interaction, "data", None) or {}
    if kind == discord.InteractionType.application_command:
        what = f"指令 /{data.get('name', '?')}"
    elif kind == discord.InteractionType.component:
        label = _COMPONENT_KINDS.get(data.get("component_type"), "元件")
        what = f"{label} {data.get('custom_id', '?')}"
        values = [str(v) for v in data.get("values") or []]
        if values:
            shown = "、".join(values[:_MAX_VALUES_SHOWN])
            more = f" 等 {len(values)} 個" if len(values) > _MAX_VALUES_SHOWN else ""
            what += f"＝{shown}{more}"
    elif kind == discord.InteractionType.modal_submit:
        what = f"填寫視窗送出 {modal_name or '（bot 這邊找不到這個視窗：可能已處理完或 bot 重開過）'}"
    else:
        return None

    user = getattr(interaction, "user", None)
    who = f"{getattr(user, 'name', '?')}（{getattr(user, 'id', '?')}）"
    where = "私訊" if getattr(interaction, "guild_id", None) is None else f"頻道 {getattr(interaction, 'channel_id', '?')}"
    message = getattr(interaction, "message", None)
    if message is not None and getattr(message, "id", None):
        where += f"｜訊息 {message.id}"
    return f"收到互動：{what}｜{who}｜{where}"


def modal_name_for(client, interaction) -> str:
    """送出的是哪個填寫視窗（類別名稱）。discord.py 沒公開這個查法，查不到就回空字串，不影響主流程。"""
    if getattr(interaction, "type", None) != discord.InteractionType.modal_submit:
        return ""
    try:
        custom_id = (interaction.data or {}).get("custom_id", "")
        modal = client._connection._view_store._modals.get(custom_id)
        return type(modal).__name__ if modal is not None else ""
    except Exception:
        return ""


def log(client, interaction) -> None:
    try:
        line = describe(interaction, modal_name_for(client, interaction))
    except Exception:
        logger.exception("互動紀錄組字失敗")
        return
    if line:
        logger.info(line)
