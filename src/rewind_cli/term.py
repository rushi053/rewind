"""Terminal helpers: colors (respecting NO_COLOR / non-TTY) and humanized values."""

import os
import sys
import time


def _color_enabled(stream=None):
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    forced = os.environ.get("REWIND_COLOR", "").lower()
    if forced in ("0", "never", "no"):
        return False
    if forced in ("1", "always", "yes") or os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("TERM") == "dumb":
        return False
    return bool(getattr(stream, "isatty", None) and stream.isatty())


class _State:
    color = None


def color_on():
    if _State.color is None:
        _State.color = _color_enabled()
        if _State.color and os.name == "nt":  # enable VT processing on Windows 10+
            os.system("")
    return _State.color


def set_color(value):
    _State.color = value


def _wrap(code):
    def apply(text):
        text = str(text)
        if not color_on():
            return text
        return "\033[%sm%s\033[0m" % (code, text)

    return apply


bold = _wrap("1")
dim = _wrap("2")
italic = _wrap("3")
red = _wrap("31")
green = _wrap("32")
yellow = _wrap("33")
blue = _wrap("34")
magenta = _wrap("35")
cyan = _wrap("36")
gray = _wrap("90")
bold_red = _wrap("1;31")
bold_green = _wrap("1;32")
bold_cyan = _wrap("1;36")
bold_yellow = _wrap("1;33")


def ago(ts, now=None):
    now = time.time() if now is None else now
    delta = max(0, now - ts)
    if delta < 45:
        return "just now"
    if delta < 3600:
        return "%dm ago" % max(1, round(delta / 60))
    if delta < 86400:
        return "%dh ago" % round(delta / 3600)
    if delta < 86400 * 30:
        return "%dd ago" % round(delta / 86400)
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def duration(seconds):
    seconds = int(round(seconds or 0))
    if seconds < 60:
        return "%ds" % seconds
    m, s = divmod(seconds, 60)
    if m < 60:
        return "%dm %02ds" % (m, s)
    h, m = divmod(m, 60)
    return "%dh %02dm" % (h, m)


def size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f TB" % n


def clock(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def stats_str(stats):
    """'+2 ~1 -3' with colors; empty components are omitted."""
    if not stats:
        return ""
    parts = []
    if stats.get("added"):
        parts.append(green("+%d" % stats["added"]))
    if stats.get("modified"):
        parts.append(yellow("~%d" % stats["modified"]))
    if stats.get("renamed"):
        parts.append(cyan(">%d" % stats["renamed"]))
    if stats.get("deleted"):
        parts.append(red("-%d" % stats["deleted"]))
    return " ".join(parts) if parts else dim("no changes")


def visible_len(text):
    import re

    return len(re.sub(r"\033\[[0-9;]*m", "", text))


def pad(text, width):
    return text + " " * max(0, width - visible_len(text))


def err(msg):
    sys.stderr.write("%s %s\n" % (bold_red("error:"), msg))


def warn(msg):
    sys.stderr.write("%s %s\n" % (bold_yellow("warning:"), msg))
