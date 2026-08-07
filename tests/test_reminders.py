from datetime import datetime, timedelta

import config
from cogs.reminders import review_reminder_due


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=config.TZ)


def test_review_reminder_waits_until_24_hours():
    created_at = NOW - timedelta(hours=23, minutes=59)

    assert not review_reminder_due(created_at, NOW, 24)


def test_review_reminder_is_due_at_24_hours():
    created_at = NOW - timedelta(hours=24)

    assert review_reminder_due(created_at, NOW, 24)


def test_review_reminder_handles_missing_creation_time():
    assert not review_reminder_due(None, NOW, 24)
