import time
from datetime import datetime

import pytest

from rewind_cli.timeparse import TimeParseError, parse_duration, parse_point


@pytest.mark.parametrize("text,seconds", [
    ("10m", 600), ("10 min", 600), ("2 hours", 7200), ("2h", 7200), ("1h30m", 5400),
    ("90s", 90), ("1 day", 86400), ("1w", 604800), ("5 minutes ago", 300),
    ("1 hour and 15 minutes", 4500), ("an hour", 3600), ("1.5h", 5400),
])
def test_durations(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("bad", ["", "10", "soon", "10 parsecs", "m10"])
def test_bad_durations(bad):
    with pytest.raises(TimeParseError):
        parse_duration(bad)


def test_point_relative():
    now = 1_000_000.0
    assert parse_point("10m", now) == now - 600


def test_point_clock_time_today_or_yesterday():
    now = datetime(2026, 10, 10, 15, 0).timestamp()
    assert parse_point("14:30", now) == datetime(2026, 10, 10, 14, 30).timestamp()
    # a clock time later than now refers to yesterday
    assert parse_point("16:00", now) == datetime(2026, 10, 9, 16, 0).timestamp()


def test_point_absolute():
    assert parse_point("2026-10-01 09:15") == datetime(2026, 10, 1, 9, 15).timestamp()
    assert parse_point("yesterday", 200000.0) == 200000.0 - 86400
