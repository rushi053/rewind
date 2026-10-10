"""Plain-language descriptions of snapshots and restore plans.

The app's main screens and the MCP tools talk about "files coming back" and
"saved versions", not manifests and snapshot ids. The ids stay available in
the structured data for anyone who opens Details.
"""

import os
import time

from .hooks import AGENT_TITLES

AGENT_TITLES_ALL = dict(AGENT_TITLES, aider="Aider", opencode="OpenCode", goose="Goose", amp="Amp")


def is_git_internal(path):
    return path == ".git" or path.startswith(".git/")


def _n(count, word):
    return "%d %s%s" % (count, word, "" if count == 1 else "s")


def describe_plan(plan):
    """Summarize a restore Plan for people. Git's own bookkeeping is counted separately."""
    come_back, change_back, removed = [], [], []
    git = 0
    for path, _entry, kind in plan.writes:
        if is_git_internal(path):
            git += 1
            continue
        (come_back if kind == "create" else change_back).append(path)
    for path in plan.deletes:
        if is_git_internal(path):
            git += 1
            continue
        removed.append(path)
    parts = []
    if come_back:
        parts.append("%s will come back" % _n(len(come_back), "file"))
    if change_back:
        parts.append("%s will change back" % _n(len(change_back), "file"))
    if removed:
        parts.append("%s will be removed" % _n(len(removed), "file"))
    if parts:
        sentence = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
        sentence = sentence[0].upper() + sentence[1:] + "."
    elif git:
        sentence = "Only Git's internal files differ (%d). Your own files already match." % git
    else:
        sentence = "Your files already match. Nothing would change."
    return {
        "sentence": sentence,
        "come_back": come_back,
        "change_back": change_back,
        "removed": removed,
        "git_internal": git,
        "total": len(plan.writes) + len(plan.deletes),
        "target_id": plan.target_id,
    }


def agent_name(sess):
    if not sess:
        return None
    return AGENT_TITLES_ALL.get(sess.get("agent") or "", None) or sess.get("name") or "An AI tool"


def _files_phrase(meta):
    changes = [c for c in (meta.get("changes") or []) if not is_git_internal(c[1])]
    total = meta.get("nchanges", len(meta.get("changes") or []))
    git = total - len(changes) if total >= len(changes) else 0
    if not changes:
        return ("Git's internal files changed" if git else "No file changes"), []
    names = [c[1] for c in changes[:3]]
    stats = {"A": 0, "M": 0, "D": 0, "R": 0}
    for c in changes:
        stats[c[0]] += 1
    bits = []
    if stats["M"]:
        bits.append("%s edited" % _n(stats["M"], "file"))
    if stats["A"]:
        bits.append("%s added" % _n(stats["A"], "file"))
    if stats["D"]:
        bits.append("%s deleted" % _n(stats["D"], "file"))
    if stats["R"]:
        bits.append("%s renamed" % _n(stats["R"], "file"))
    return ", ".join(bits), names


def describe_snapshot(meta, sessions=None):
    """{"title", "summary", "files", "who", "kind"} for one saved version."""
    sessions = sessions or {}
    sess = sessions.get(meta.get("session")) if meta.get("session") else None
    who = agent_name(sess)
    trig = meta.get("trigger")
    msg = meta.get("message") or ""
    summary, files = _files_phrase(meta)
    kind = "save"
    if trig == "init":
        title = "Started protecting this folder"
        summary = "%s saved" % _n(meta.get("files", 0), "file")
        files = []
    elif trig == "manual":
        title = "Checkpoint: %s" % msg if msg else "Checkpoint saved"
    elif trig == "watch":
        title = "%s changed files" % who if who else "Changes saved automatically"
    elif trig == "hook":
        title = "%s changed files" % (who or "An AI tool")
        detail = meta.get("detail")
        if meta.get("tool") in ("Bash", "Shell", "run_shell_command") and detail and msg.startswith("before"):
            title = "Saved before %s ran a command" % (who or "an AI tool")
    elif trig == "guard":
        if msg.startswith("blocked"):
            title = "Blocked a dangerous command" + (" from %s" % who if who else "")
            summary = msg[len("blocked: "):] if msg.startswith("blocked: ") else msg
            files = []
            kind = "blocked"
        else:
            title = "Saved before a risky command"
            kind = "warning"
    elif trig == "safety":
        title = "Saved your files before going back"
        kind = "safety"
    elif trig == "restore":
        kind = "restore"
        if msg.startswith("undo session"):
            title = "Undid an AI session"
        elif msg.startswith("undo"):
            title = "Undid the last change"
        elif msg.startswith("redo"):
            title = "Redid a change"
        elif msg.startswith("recover"):
            title = "Brought back deleted files"
        else:
            title = "Went back in time"
    elif trig == "run-start":
        title = "%s started" % (who or msg.replace("before ", "") or "A session")
    elif trig == "run-end":
        title = "%s finished" % (who or msg.replace("after ", "") or "A session")
    else:
        title = msg or "Saved a version"
    return {"id": meta["id"], "time": meta["time"], "title": title, "summary": summary, "files": files,
            "who": who, "kind": kind, "detail": meta.get("detail"), "session": meta.get("session")}


def when(ts, now=None):
    """'2 minutes ago', 'yesterday at 14:05', 'Mar 3 at 09:12'."""
    now = time.time() if now is None else now
    d = max(0, now - ts)
    if d < 45:
        return "just now"
    if d < 3600:
        m = max(1, int(round(d / 60)))
        return "%d minute%s ago" % (m, "" if m == 1 else "s")
    lt, ln = time.localtime(ts), time.localtime(now)
    clock = time.strftime("%H:%M", lt)
    if lt.tm_yday == ln.tm_yday and lt.tm_year == ln.tm_year:
        return "today at %s" % clock
    if (now - ts) < 2 * 86400 and time.localtime(now - 86400).tm_yday == lt.tm_yday:
        return "yesterday at %s" % clock
    return "%s at %s" % (time.strftime("%b %d", lt).replace(" 0", " "), clock)


def short_path(path, width=60):
    if len(path) <= width:
        return path
    head, tail = os.path.split(path)
    return "…/" + tail if len(tail) < width - 2 else "…" + path[-(width - 1):]
