"""跨 cog 共用的 Discord 訊息文字 helper（純函式，沿用 channel_guard 的共用模組模式）。"""
from datetime import datetime

from services import repositories

DISCORD_MESSAGE_LIMIT = 1900  # 留 buffer 的 2000 字上限


def trim_message(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> str:
    if len(text) <= limit:
        return text
    suffix = "\n...清單過長已截斷"
    truncated = text[:limit - len(suffix)]
    # 盡量在整行邊界截斷，避免把一行切一半留下破碎的文字。
    cut = truncated.rfind("\n")
    if cut > 0:
        truncated = truncated[:cut]
    return truncated.rstrip() + suffix


def fit_items(items: list[str], budget: int) -> tuple[list[str], int]:
    """在長度預算內盡量放入整行項目；回傳 (放得下的行, 放不下的筆數)。

    給「有下拉選單可以看到完整資料、文字只是預覽」的畫面用：寧可少列幾筆並誠實
    說明還有幾筆，也不要讓 Discord 訊息上限把最後一行切成破碎文字。
    """
    kept: list[str] = []
    used = 0
    for item in items:
        cost = len(item) + 1
        if kept and used + cost > budget:
            break
        kept.append(item)
        used += cost
    return kept, len(items) - len(kept)


def paginate_blocks(blocks: list[list[str]], budget: int) -> list[list[str]]:
    """把多個「不可切開的區塊」依長度預算分頁；單一區塊超過預算時自己成一頁。"""
    pages: list[list[str]] = []
    current: list[str] = []
    used = 0
    for block in blocks:
        cost = len("\n".join(block)) + 1
        if current and used + cost > budget:
            pages.append(current)
            current, used = [], 0
        current.extend(block)
        used += cost
    if current:
        pages.append(current)
    return pages or [[]]


def paginate_sections(sections: list[tuple[str, list[str]]], budget: int) -> list[list[str]]:
    """把「分區標題＋內容行」切成多頁。

    分區內容太長跨頁時，新的一頁會重印標題並標示「（續）」，避免出現沒有標題的孤兒
    清單；標題也不會單獨留在上一頁的結尾。
    """
    pages: list[list[str]] = []
    current: list[str] = []
    used = 0
    for title, items in sections:
        pending_title = title
        for item in items:
            item_cost = len(item) + 1
            title_cost = len(pending_title) + 1 if pending_title else 0
            if current and used + title_cost + item_cost > budget:
                pages.append(current)
                current, used = [], 0
                pending_title = f"{title}（續）"
                title_cost = len(pending_title) + 1
            if pending_title:
                current.append(pending_title)
                used += title_cost
                pending_title = ""
            current.append(item)
            used += item_cost
        if current:
            current.append("")
            used += 1
    if current:
        pages.append(current)
    return pages or [[]]


def deadline_text(task: repositories.TaskItem, now: datetime) -> str:
    """任務截止的人話描述：截止 7/15（剩 3 天）／（今天）／（已過 2 天）。"""
    is_revision = task.status == "需修改" and task.revision_deadline is not None
    if is_revision:
        deadline, has_time = task.revision_deadline, True
    else:
        deadline, has_time = repositories.task_action_deadline(task)
    if not deadline:
        return "無截止日"
    day_diff = (deadline.date() - now.date()).days
    date_text = f"{deadline.month}/{deadline.day}"
    if has_time:
        date_text += deadline.strftime(" %H:%M")
    prefix = "修改期限" if is_revision else "截止"
    if day_diff < 0:
        return f"{prefix} {date_text}（已過 {-day_diff} 天）"
    if day_diff == 0:
        return f"{prefix} {date_text}（今天）"
    return f"{prefix} {date_text}（剩 {day_diff} 天）"
