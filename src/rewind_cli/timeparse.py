"""Parse human time expressions for `rewind back`: '10m', '2 hours', '1h30m', '14:05'."""

import re
import time
from datetime import datetime, timedelta

UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
}

_PART = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]+)")


class TimeParseError(ValueError):
    pass


def parse_duration(text):
    """'10m' -> 600, '2 hours' -> 7200, '1h30m' -> 5400, '1 day 2 hours ago' -> 93600."""
    s = text.strip().lower()
    s = re.sub(r"\bago\b", "", s).replace(",", " ")
    s = re.sub(r"\band\b", " ", s).strip()
    if not s:
        raise TimeParseError("empty duration")
    if re.fullmatch(r"\d+(\.\d+)?", s):
        raise TimeParseError("%r needs a unit, e.g. %sm or %sh" % (text, s, s))
    if s in ("an hour", "a hour"):
        return 3600
    if s in ("a minute",):
        return 60
    if s in ("a day",):
        return 86400
    pos = 0
    total = 0.0
    found = False
    for m in _PART.finditer(s):
        if s[pos:m.start()].strip():
            raise TimeParseError("could not understand %r" % text)
        unit = m.group(2)
        if unit not in UNITS:
            raise TimeParseError("unknown unit %r in %r" % (unit, text))
        total += float(m.group(1)) * UNITS[unit]
        pos = m.end()
        found = True
    if not found or s[pos:].strip():
        raise TimeParseError("could not understand %r (try 10m, 2h, '2 hours', 14:30)" % text)
    return total


def parse_point(text, now=None):
    """Return a unix timestamp for a relative duration or an absolute clock time/date."""
    now = time.time() if now is None else now
    s = text.strip()
    low = s.lower()
    if low == "yesterday":
        return now - 86400
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if m:
        base = datetime.fromtimestamp(now)
        t = base.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=int(m.group(3) or 0), microsecond=0)
        if t > base:
            t -= timedelta(days=1)  # '14:30' in the future means yesterday 14:30
        return t.timestamp()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            pass
    return now - parse_duration(s)
