"""Best-effort desktop notifications (osascript on macOS, notify-send on Linux).

Notifications never block and never raise: a missing notifier simply means no
popup. Every seatbelt block is also written to the in-app activity feed, which
is the record that matters.
"""

import os
import shutil
import subprocess
import sys

from . import registry


def _applescript_quote(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def command_for(title, message, platform=None, which=shutil.which):
    """The argv that would show a notification on this platform, or None."""
    platform = platform or sys.platform
    title = " ".join(str(title).split())[:120]
    message = " ".join(str(message).split())[:300]
    if platform == "darwin":
        if which("osascript"):
            script = "display notification %s with title %s" % (_applescript_quote(message), _applescript_quote(title))
            return ["osascript", "-e", script]
        return None
    if platform.startswith("linux") or "bsd" in platform:
        exe = which("notify-send")
        if exe:
            return [exe, "--app-name=Rewind", "--icon=dialog-warning", title, message]
        return None
    return None  # Windows: no stdlib-only toast API; the activity feed still records it


def desktop(title, message):
    """Show a desktop notification if the user allows it. Returns True if one was launched."""
    if os.environ.get("REWIND_NO_NOTIFY"):
        return False
    try:
        if not registry.settings().get("notifications", True):
            return False
        argv = command_for(title, message)
        if not argv:
            return False
        kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.name != "nt":
            kwargs["start_new_session"] = True
        subprocess.Popen(argv, **kwargs)
        return True
    except Exception:
        return False


def seatbelt_blocked(repo, agent_title, command, reasons):
    """Record a seatbelt block in the activity feed and pop a notification."""
    folder = str(repo.root)
    short = " ".join((command or "").split())
    if len(short) > 80:
        short = short[:79] + "…"
    registry.log("blocked", "Blocked a dangerous command from %s" % agent_title, folder=folder,
                 detail=command, level="warning", reasons=reasons)
    desktop("Rewind blocked a command", "%s tried: %s (%s)" % (agent_title, short, "; ".join(reasons)[:120]))
