"""共同時段比對：N 位參與者時段交集的純函式，可獨立單元測試。"""
from datetime import datetime, timedelta

Interval = tuple[datetime, datetime]


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    """把同一人的多段時段做聯集（重疊或相接的合併），回傳依開始時間排序。"""
    valid = [(s, e) for s, e in intervals if s < e]
    if not valid:
        return []
    valid.sort()
    merged = [valid[0]]
    for start, end in valid[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def intersect_two(a: list[Interval], b: list[Interval]) -> list[Interval]:
    """兩組（各自已排序、無重疊）時段的交集。"""
    result: list[Interval] = []
    i = j = 0
    while i < len(a) and j < len(b):
        start = max(a[i][0], b[j][0])
        end = min(a[i][1], b[j][1])
        if start < end:
            result.append((start, end))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return result


def find_common_windows(
    participants: dict[str, list[Interval]],
    duration_minutes: int,
) -> list[Interval]:
    """回傳「全員都可用」且長度 >= duration_minutes 的時間窗，依開始時間排序。

    participants: {參與者名稱: [(start, end), ...]}
    只要有任何一位沒有時段，結果即為空（全員交集）。
    """
    if not participants:
        return []
    merged_lists = [merge_intervals(ivs) for ivs in participants.values()]
    common = merged_lists[0]
    for other in merged_lists[1:]:
        common = intersect_two(common, other)
        if not common:
            return []
    min_len = timedelta(minutes=duration_minutes)
    return [(s, e) for s, e in common if e - s >= min_len]


def missing_participants(
    all_names: list[str],
    filled: dict[str, list[Interval]],
) -> list[str]:
    """回傳尚未填寫任何時段的參與者名單。"""
    return [n for n in all_names if not filled.get(n)]
