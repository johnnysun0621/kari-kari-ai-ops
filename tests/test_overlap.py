"""overlap.py 與 timeparse.py 的單元測試。"""
from datetime import datetime, timedelta

import pytest

import config
from services.overlap import find_common_windows, intersect_two, merge_intervals, missing_participants
from services.timeparse import ParseError, format_candidate, parse_candidates_text, parse_slot


def t(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=config.TZ)


class TestMergeIntervals:
    def test_empty(self):
        assert merge_intervals([]) == []

    def test_invalid_dropped(self):
        assert merge_intervals([(t(1, 10), t(1, 10)), (t(1, 12), t(1, 11))]) == []

    def test_overlapping_merged(self):
        result = merge_intervals([(t(1, 10), t(1, 14)), (t(1, 12), t(1, 16))])
        assert result == [(t(1, 10), t(1, 16))]

    def test_touching_merged(self):
        result = merge_intervals([(t(1, 10), t(1, 12)), (t(1, 12), t(1, 14))])
        assert result == [(t(1, 10), t(1, 14))]

    def test_disjoint_kept(self):
        result = merge_intervals([(t(2, 10), t(2, 12)), (t(1, 10), t(1, 12))])
        assert result == [(t(1, 10), t(1, 12)), (t(2, 10), t(2, 12))]


class TestIntersectTwo:
    def test_partial_overlap(self):
        a = [(t(1, 10), t(1, 14))]
        b = [(t(1, 12), t(1, 16))]
        assert intersect_two(a, b) == [(t(1, 12), t(1, 14))]

    def test_no_overlap(self):
        assert intersect_two([(t(1, 10), t(1, 12))], [(t(1, 12), t(1, 14))]) == []

    def test_multiple_segments(self):
        a = [(t(1, 9), t(1, 12)), (t(1, 14), t(1, 18))]
        b = [(t(1, 10), t(1, 16))]
        assert intersect_two(a, b) == [(t(1, 10), t(1, 12)), (t(1, 14), t(1, 16))]


class TestFindCommonWindows:
    def test_three_people_common(self):
        participants = {
            "A": [(t(15, 14), t(15, 20))],
            "B": [(t(15, 12), t(15, 18))],
            "C": [(t(15, 15), t(15, 22))],
        }
        assert find_common_windows(participants, 60) == [(t(15, 15), t(15, 18))]

    def test_duration_filter(self):
        participants = {
            "A": [(t(15, 14), t(15, 16))],
            "B": [(t(15, 15), t(15, 18))],
        }
        # 交集 15:00–16:00 只有 60 分鐘，需求 90 分鐘 → 不合格
        assert find_common_windows(participants, 90) == []
        assert find_common_windows(participants, 60) == [(t(15, 15), t(15, 16))]

    def test_one_person_empty_means_no_result(self):
        participants = {"A": [(t(15, 14), t(15, 20))], "B": []}
        assert find_common_windows(participants, 60) == []

    def test_cross_day(self):
        participants = {
            "A": [(t(15, 22), t(16, 2))],
            "B": [(t(15, 23), t(16, 3))],
        }
        assert find_common_windows(participants, 120) == [(t(15, 23), t(16, 2))]

    def test_multiple_windows_sorted(self):
        participants = {
            "A": [(t(16, 10), t(16, 12)), (t(15, 10), t(15, 12))],
            "B": [(t(15, 9), t(16, 23))],
        }
        result = find_common_windows(participants, 60)
        assert result == [(t(15, 10), t(15, 12)), (t(16, 10), t(16, 12))]

    def test_missing_participants(self):
        filled = {"A": [(t(15, 10), t(15, 12))], "B": []}
        assert missing_participants(["A", "B", "C"], filled) == ["B", "C"]


class TestParseSlot:
    NOW = datetime(2026, 7, 7, 12, 0, tzinfo=config.TZ)

    def test_basic(self):
        start, end = parse_slot("7/15", "14:00", "18:00", self.NOW)
        assert start == t(15, 14) and end == t(15, 18)

    def test_full_date_and_loose_time(self):
        start, end = parse_slot("2026-07-15", "14", "1830", self.NOW)
        assert start == t(15, 14) and end == t(15, 18, 30)

    def test_cross_midnight(self):
        start, end = parse_slot("7/15", "22:00", "01:00", self.NOW)
        assert start == t(15, 22) and end == t(16, 1)

    def test_past_slot_rejected(self):
        with pytest.raises(ParseError):
            parse_slot("7/1", "10:00", "12:00", self.NOW)

    def test_past_date_without_year_rejected(self):
        # 無年份的過去日期直接報錯，避免打錯月日默默變成明年的時段
        with pytest.raises(ParseError):
            parse_slot("1/5", "10:00", "12:00", self.NOW)

    def test_next_year_with_full_date_ok(self):
        start, _ = parse_slot("2027-01-05", "10:00", "12:00", self.NOW)
        assert start.year == 2027

    def test_garbage_rejected(self):
        with pytest.raises(ParseError):
            parse_slot("下週二", "14:00", "18:00", self.NOW)


class TestCandidatesText:
    def test_roundtrip(self):
        pairs = [(t(15, 14), t(15, 18)), (t(15, 22), t(16, 1))]
        text = "\n".join(format_candidate(s, e) for s, e in pairs)
        assert parse_candidates_text(text) == pairs

    def test_ignores_noise_lines(self):
        text = "候選：\n2026-07-15 14:00~18:00\n（待老師確認）"
        assert parse_candidates_text(text) == [(t(15, 14), t(15, 18))]
