"""Modal 輸入的日期時間解析與候選時段文字格式。"""
import re
from datetime import datetime, time, timedelta

import config


class ParseError(ValueError):
    """使用者輸入無法解析，訊息可直接顯示給使用者。"""


def parse_date(text: str, now: datetime | None = None) -> datetime:
    """解析 '7/15'、'07-15'、'2026-07-15'、'2026/7/15' 為當地時區的當日 00:00。

    未給年份時視為今年；若該日期已過去則報錯（跨年請填完整年份，避免打錯月日
    默默變成一年後的時段）。
    """
    now = now or datetime.now(config.TZ)
    text = text.strip().replace("／", "/").replace("－", "-")
    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})[-/](\d{1,2})", text)
        if not m:
            raise ParseError(f"看不懂日期「{text}」，請用 7/15 或 2026-07-15 格式")
        mo, d = map(int, m.groups())
        y = now.year
    try:
        dt = datetime(y, mo, d, tzinfo=config.TZ)
    except ValueError:
        raise ParseError(f"日期「{text}」不存在，請確認月份與日期")
    if len(m.groups()) == 2 and dt.date() < now.date():
        raise ParseError(
            f"日期「{text}」已經過去了；如果是明年的日期，請填完整年份（例：{y + 1}-{mo:02d}-{d:02d}）"
        )
    return dt


def parse_time(text: str) -> tuple[int, int]:
    """解析 '14:00'、'14'、'1430'、'下午2點' 不支援；標準格式為 HH:MM。"""
    text = text.strip().replace("：", ":")
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
    elif re.fullmatch(r"\d{1,2}", text):
        h, mi = int(text), 0
    elif re.fullmatch(r"\d{4}", text):
        h, mi = int(text[:2]), int(text[2:])
    else:
        raise ParseError(f"看不懂時間「{text}」，請用 14:00 格式")
    if not (0 <= h <= 24 and 0 <= mi <= 59):
        raise ParseError(f"時間「{text}」超出範圍")
    return h, mi


def parse_slot(
    date_text: str,
    start_text: str,
    end_text: str,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """組合日期 + 開始/結束時間為一個時段，含基本驗證。

    結束時間可為 24:00 或小於開始（視為跨日到隔天）。
    """
    now = now or datetime.now(config.TZ)
    day = parse_date(date_text, now)
    sh, sm = parse_time(start_text)
    eh, em = parse_time(end_text)
    if sh == 24:
        raise ParseError("開始時間不能是 24:00")
    start = day + timedelta(hours=sh, minutes=sm)
    end = day + timedelta(hours=eh, minutes=em)
    if end <= start:
        end += timedelta(days=1)  # 跨日，例如 22:00~01:00
    if end - start > timedelta(hours=18):
        raise ParseError("單一時段超過 18 小時，請確認開始／結束時間")
    if end <= now:
        raise ParseError("這個時段已經過去了，請填未來的時間")
    return start, end


# ---------- 候選時段文字格式（存進 Notion「候選時段」text 欄位） ----------

def format_candidate(start: datetime, end: datetime) -> str:
    return f"{start.strftime('%Y-%m-%d %H:%M')}~{end.strftime('%H:%M') if start.date() == end.date() else end.strftime('%Y-%m-%d %H:%M')}"


def parse_candidates_text(text: str) -> list[tuple[datetime, datetime]]:
    """把「候選時段」欄位的多行文字解析回時間區間。"""
    result = []
    for line in text.splitlines():
        line = line.strip().lstrip("・-• ").strip()
        if not line:
            continue
        m = re.fullmatch(
            r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})~(?:(\d{4}-\d{2}-\d{2}) )?(\d{2}:\d{2})",
            line,
        )
        if not m:
            continue
        d1, t1, d2, t2 = m.groups()
        start = datetime.strptime(f"{d1} {t1}", "%Y-%m-%d %H:%M").replace(tzinfo=config.TZ)
        end = datetime.strptime(f"{d2 or d1} {t2}", "%Y-%m-%d %H:%M").replace(tzinfo=config.TZ)
        if end <= start:
            end += timedelta(days=1)
        result.append((start, end))
    return result


WEEKDAY_ZH = "一二三四五六日"


def humanize(start: datetime, end: datetime) -> str:
    """人類可讀格式：7/15（週二）14:00–18:00"""
    wd = WEEKDAY_ZH[start.weekday()]
    if end.date() == start.date() + timedelta(days=1) and end.time() == time(0, 0):
        return f"{start.month}/{start.day}（週{wd}）{start.strftime('%H:%M')}–24:00"
    if start.date() == end.date():
        return f"{start.month}/{start.day}（週{wd}）{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
    wd2 = WEEKDAY_ZH[end.weekday()]
    return f"{start.month}/{start.day}（週{wd}）{start.strftime('%H:%M')} – {end.month}/{end.day}（週{wd2}）{end.strftime('%H:%M')}"
