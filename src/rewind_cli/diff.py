"""Comparing manifests and rendering file diffs."""

import difflib

from . import term

MAX_DIFF_BYTES = 2 * 1024 * 1024


def same_entry(a, b):
    if a is None or b is None:
        return False
    if "l" in a or "l" in b:
        return a.get("l") == b.get("l")
    return a.get("h") == b.get("h") and a.get("m") == b.get("m")


def diff_manifests(old, new):
    """Return a sorted list of changes: dicts with status A/M/D/R, path, old_path, old, new."""
    changes = []
    added, deleted = {}, {}
    for path, entry in new.items():
        prev = old.get(path)
        if prev is None:
            added[path] = entry
        elif not same_entry(prev, entry):
            changes.append({"status": "M", "path": path, "old": prev, "new": entry})
    for path, entry in old.items():
        if path not in new:
            deleted[path] = entry
    # rename detection: identical content that vanished in one place and appeared in another
    by_hash = {}
    for path, entry in deleted.items():
        key = entry.get("h") or ("link:" + str(entry.get("l")))
        by_hash.setdefault(key, []).append(path)
    for path, entry in sorted(added.items()):
        key = entry.get("h") or ("link:" + str(entry.get("l")))
        candidates = by_hash.get(key)
        if candidates:
            src = candidates.pop(0)
            changes.append({"status": "R", "path": path, "old_path": src, "old": deleted.pop(src), "new": entry})
        else:
            changes.append({"status": "A", "path": path, "old": None, "new": entry})
    for path, entry in deleted.items():
        changes.append({"status": "D", "path": path, "old": entry, "new": None})
    changes.sort(key=lambda c: c["path"])
    return changes


def summarize(changes):
    s = {"added": 0, "modified": 0, "deleted": 0, "renamed": 0}
    key = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed"}
    for c in changes:
        s[key[c["status"]]] += 1
    return s


def line_delta(repo, change):
    """Added and removed line counts for one text change.

    Returns None for binary files, files too large to diff, and symlink-only
    changes. A rename with the same text is ``{"plus": 0, "minus": 0}``.
    """
    a = entry_bytes(repo, change.get("old"))
    b = entry_bytes(repo, change.get("new"))
    if is_binary(a) or is_binary(b) or len(a) > MAX_DIFF_BYTES or len(b) > MAX_DIFF_BYTES:
        return None
    if "l" in (change.get("old") or {}) or "l" in (change.get("new") or {}):
        return None
    al, bl = decode(a).splitlines(), decode(b).splitlines()
    plus = minus = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, al, bl, autojunk=False).get_opcodes():
        if tag == "insert":
            plus += j2 - j1
        elif tag == "delete":
            minus += i2 - i1
        elif tag == "replace":
            minus += i2 - i1
            plus += j2 - j1
    return {"plus": plus, "minus": minus}


def is_binary(data):
    return b"\0" in data[:8192]


def entry_bytes(repo, entry):
    if entry is None:
        return b""
    if "l" in entry:
        return ("symlink -> %s\n" % entry["l"]).encode("utf-8")
    return repo.get_bytes(entry["h"])


def decode(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def unified(repo, change, context=3):
    """Return a list of plain unified-diff lines for one change (no color)."""
    old_path = change.get("old_path") or change["path"]
    a = entry_bytes(repo, change.get("old"))
    b = entry_bytes(repo, change.get("new"))
    header = ["--- %s" % ("a/" + old_path if change.get("old") else "/dev/null"),
              "+++ %s" % ("b/" + change["path"] if change.get("new") else "/dev/null")]
    if change["status"] == "R" and a == b:
        return header + ["rename %s -> %s (content unchanged)" % (old_path, change["path"])]
    if is_binary(a) or is_binary(b):
        return header + ["Binary files differ (%d -> %d bytes)" % (len(a), len(b))]
    if len(a) > MAX_DIFF_BYTES or len(b) > MAX_DIFF_BYTES:
        return header + ["File too large to diff (%d -> %d bytes)" % (len(a), len(b))]
    old, new = change.get("old") or {}, change.get("new") or {}
    lines = []
    if a == b and old.get("m") != new.get("m"):
        return header + ["mode %o -> %o" % (old.get("m", 0), new.get("m", 0))]
    for line in difflib.unified_diff(decode(a).splitlines(), decode(b).splitlines(), lineterm="", n=context):
        if line.startswith("---") or line.startswith("+++"):
            continue
        lines.append(line)
    return header + lines


def colorize(lines):
    out = []
    for line in lines:
        if line.startswith("---") or line.startswith("+++"):
            out.append(term.bold(line))
        elif line.startswith("@@"):
            out.append(term.cyan(line))
        elif line.startswith("+"):
            out.append(term.green(line))
        elif line.startswith("-"):
            out.append(term.red(line))
        elif line.startswith(("Binary", "rename", "mode", "File too large")):
            out.append(term.dim(line))
        else:
            out.append(line)
    return out


def rows(repo, change, context=3):
    """Structured diff for the web UI: a list of hunks of aligned rows."""
    a = entry_bytes(repo, change.get("old"))
    b = entry_bytes(repo, change.get("new"))
    if is_binary(a) or is_binary(b):
        return {"binary": True, "old_size": len(a), "new_size": len(b), "hunks": []}
    if len(a) > MAX_DIFF_BYTES or len(b) > MAX_DIFF_BYTES:
        return {"too_large": True, "old_size": len(a), "new_size": len(b), "hunks": []}
    al, bl = decode(a).splitlines(), decode(b).splitlines()
    sm = difflib.SequenceMatcher(None, al, bl, autojunk=False)
    hunks = []
    for group in sm.get_grouped_opcodes(context):
        hunk = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for k in range(i2 - i1):
                    hunk.append(["eq", i1 + k + 1, al[i1 + k], j1 + k + 1, bl[j1 + k]])
            else:
                n = max(i2 - i1, j2 - j1)
                for k in range(n):
                    left = (i1 + k + 1, al[i1 + k]) if i1 + k < i2 else (None, None)
                    right = (j1 + k + 1, bl[j1 + k]) if j1 + k < j2 else (None, None)
                    kind = "chg" if left[0] and right[0] else ("del" if left[0] else "add")
                    hunk.append([kind, left[0], left[1], right[0], right[1]])
        hunks.append(hunk)
    return {"binary": False, "hunks": hunks, "old_lines": len(al), "new_lines": len(bl)}
