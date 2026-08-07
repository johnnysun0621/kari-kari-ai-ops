from datetime import datetime

import config
from cogs import scheduling_admin


def test_duration_candidates_use_requested_duration_and_30_minute_steps():
    start = datetime(2026, 8, 13, 13, 30, tzinfo=config.TZ)
    end = datetime(2026, 8, 13, 18, 0, tzinfo=config.TZ)

    candidates = scheduling_admin._duration_candidates([(start, end)], 60)

    assert candidates[0] == (start, datetime(2026, 8, 13, 14, 30, tzinfo=config.TZ))
    assert candidates[-1] == (
        datetime(2026, 8, 13, 17, 0, tzinfo=config.TZ),
        datetime(2026, 8, 13, 18, 0, tzinfo=config.TZ),
    )
    assert len(candidates) == 8


def test_candidate_result_is_grouped_by_date():
    candidates = [
        (datetime(2026, 8, 13, 13, 30, tzinfo=config.TZ), datetime(2026, 8, 13, 14, 30, tzinfo=config.TZ)),
        (datetime(2026, 8, 13, 14, 0, tzinfo=config.TZ), datetime(2026, 8, 13, 15, 0, tzinfo=config.TZ)),
        (datetime(2026, 8, 14, 13, 30, tzinfo=config.TZ), datetime(2026, 8, 14, 14, 30, tzinfo=config.TZ)),
    ]

    lines = scheduling_admin._format_candidate_groups(candidates)

    assert lines == [
        "**8/13（週四）**",
        "　13:30–14:30　14:00–15:00",
        "**8/14（週五）**",
        "　13:30–14:30",
    ]
