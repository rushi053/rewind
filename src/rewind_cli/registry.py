"""The global list of folders Rewind protects, plus app settings and the activity feed.

Everything lives under ``~/.rewind`` (or ``$REWIND_HOME``)::

    registry.json    {"folders": [{"id", "path", "name", "added", "protected"}]}
    settings.json    app-wide settings (notifications, onboarding)
    activity.jsonl   one JSON event per line, newest last (trimmed automatically)

A folder's own history and configuration stay in its store
(``~/.rewind/projects/<hash>`` by default, or ``.rewind/`` inside the folder
when that store already exists or the user opted in); the registry only remembers *which* folders the
app shows and whether the user wants them watched.
"""

import json
import os
import sys
import time
from pathlib import Path

from .store import Lock, NotInitialized, Repo, RewindError, project_hash, read_json, rewind_home, write_json

SETTINGS_DEFAULTS = {"notifications": True, "onboarded": False}
ACTIVITY_MAX_BYTES = 512 * 1024
ACTIVITY_KEEP = 400

# folders that are far too broad to poll, or that belong to the OS
_REFUSE = {"/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/sbin", "/sys", "/usr", "/var",
           "/System", "/Library", "/Applications", "/private", "/Volumes", "/Users", "/home", "/opt", "/tmp"}


def _home_dir():
    return rewind_home()


def _registry_path():
    return _home_dir() / "registry.json"


def _lock():
    _home_dir().mkdir(parents=True, exist_ok=True)
    return Lock(_home_dir() / "registry.lock", timeout=10)


def folder_id(path):
    return project_hash(path)


def _norm(path):
    return str(Path(os.path.expanduser(str(path))).resolve())


# ------------------------------------------------------------------ registry
def load():
    data = read_json(_registry_path(), {}) or {}
    folders = [f for f in data.get("folders", []) if isinstance(f, dict) and f.get("path")]
    return folders


def _save(folders):
    _home_dir().mkdir(parents=True, exist_ok=True)
    write_json(_registry_path(), {"version": 1, "folders": folders}, indent=2)


def get(fid):
    for f in load():
        if f["id"] == fid:
            return f
    raise RewindError("that folder is not in Rewind's list (it may have been removed)")


def find_by_path(path):
    p = _norm(path)
    for f in load():
        if f["path"] == p:
            return f
    return None


def register(path, protected=False, source="app"):
    """Add a folder to the registry (no-op if present). Returns the entry."""
    p = _norm(path)
    with _lock():
        folders = load()
        for f in folders:
            if f["path"] == p:
                return f
        entry = {"id": folder_id(p), "path": p, "name": Path(p).name or p, "added": time.time(),
                 "protected": bool(protected), "source": source}
        folders.append(entry)
        _save(folders)
        return entry


def update(fid, **changes):
    with _lock():
        folders = load()
        for f in folders:
            if f["id"] == fid:
                f.update(changes)
                _save(folders)
                return f
    raise RewindError("that folder is not in Rewind's list")


def unregister(fid):
    with _lock():
        folders = load()
        keep = [f for f in folders if f["id"] != fid]
        if len(keep) == len(folders):
            raise RewindError("that folder is not in Rewind's list")
        _save(keep)


def note_watch_state(root, on):
    """Keep the registry's intent in sync when the CLI starts/stops a watcher."""
    entry = find_by_path(root)
    if entry is not None and entry.get("protected") != bool(on):
        try:
            update(entry["id"], protected=bool(on))
        except RewindError:
            pass


# ---------------------------------------------------------------- validation
def check_folder(path):
    """Return {"ok", "path", "error", "warning", "already"} for a folder the user wants to protect."""
    raw = (path or "").strip()
    out = {"ok": False, "path": raw, "error": None, "warning": None, "already": None, "container": False}
    if not raw:
        out["error"] = "Choose a folder first."
        return out
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        out["error"] = "Use a full path, like %s" % (Path.home() / "code" / "my-project")
        return out
    try:
        p = p.resolve()
    except OSError as exc:
        out["error"] = "Can't open that folder: %s" % exc
        return out
    out["path"] = str(p)
    if not p.exists():
        out["error"] = "That folder doesn't exist."
        return out
    if not p.is_dir():
        out["error"] = "That's a file, not a folder."
        return out
    s = str(p)
    home = str(Path.home().resolve())
    if s in _REFUSE or (os.name == "nt" and len(s) <= 3):
        out["error"] = "That's a system folder. Pick a project folder inside it instead."
        out["container"] = True
        return out
    if s == home:
        out["error"] = "Your whole home folder is too big to watch. Pick a project folder inside it."
        out["container"] = True
        return out
    if os.access(s, os.W_OK) is False:
        out["error"] = "Rewind can't write to that folder."
        return out
    existing = find_by_path(s)
    if existing:
        out["already"] = existing["id"]
        out["error"] = "This folder is already in Rewind."
        return out
    for f in load():
        fp = f["path"]
        if s.startswith(fp.rstrip(os.sep) + os.sep):
            out["error"] = "This folder is inside %s, which Rewind already protects." % f["name"]
            return out
        if fp.startswith(s.rstrip(os.sep) + os.sep):
            out["error"] = "This folder contains %s, which Rewind already protects. Pick a project folder inside it." % f["name"]
            out["container"] = True
            return out
    # a rough size check so nobody points the watcher at a photo library by accident
    try:
        count = 0
        for _dirpath, dirnames, filenames in os.walk(s):
            dirnames[:] = [d for d in dirnames if d not in ("node_modules", ".git", ".venv", "venv", "__pycache__", "target", "dist", "build")]
            count += len(filenames)
            if count > 50000:
                out["warning"] = "This folder has more than 50,000 files. Rewind will work, but saving may be slow."
                break
    except OSError:
        pass
    out["ok"] = True
    return out


def setup_folder(path, global_store=None):
    """Initialise a Rewind store for ``path`` if needed and write the default ignore file.

    Returns the Repo. Does not take the first snapshot (that can be slow).
    """
    from .ignore import DEFAULT_IGNORE

    root = _norm(path)
    try:
        repo = Repo.find(root)
        if os.path.samefile(str(repo.root), root):
            return repo
    except NotInitialized:
        pass
    repo = Repo.init(root, global_store=global_store)
    ignore = repo.root / ".rewindignore"
    if not ignore.exists():
        ignore.write_text(DEFAULT_IGNORE, "utf-8")
    return repo


def repo_for(entry):
    root = entry["path"]
    if not Path(root).is_dir():
        raise RewindError("the folder %s no longer exists" % root)
    repo = Repo.find(root)
    if not os.path.samefile(str(repo.root), root):
        raise NotInitialized("%s is not set up with Rewind" % root)
    return repo


# --------------------------------------------------------------- protection
def set_protected(fid, on):
    """Start or stop the background watcher for a folder and remember the choice."""
    from .watch import daemon_pid, start_daemon, stop_daemon

    entry = get(fid)
    repo = repo_for(entry)
    if on:
        if not daemon_pid(repo):
            start_daemon(repo)
    else:
        stop_daemon(repo)
    update(fid, protected=bool(on))
    log("protected" if on else "paused", folder=entry,
        title=("Protection turned on for %s" if on else "Protection paused for %s") % entry["name"])
    return entry


def ensure_watchers():
    """Start watchers for every folder marked protected whose watcher isn't running."""
    from .watch import daemon_pid, start_daemon

    started = []
    for entry in load():
        if not entry.get("protected"):
            continue
        try:
            repo = repo_for(entry)
        except (RewindError, OSError):
            continue
        if not daemon_pid(repo):
            try:
                start_daemon(repo)
                started.append(entry["id"])
            except (RewindError, OSError) as exc:
                sys.stderr.write("rewind app: could not start watcher for %s: %s\n" % (entry["path"], exc))
    return started


_SIZE_CACHE = {}


def store_bytes(repo, ttl=20.0):
    key = str(repo.store)
    hit = _SIZE_CACHE.get(key)
    now = time.time()
    if hit and now - hit[0] < ttl:
        return hit[1]
    _count, total = repo.store_size()
    _SIZE_CACHE[key] = (now, total)
    return total


def status(entry):
    """Everything the app shows on a folder card."""
    from .hooks import AGENTS, installed
    from .watch import daemon_pid

    out = dict(entry)
    out.update({"exists": Path(entry["path"]).is_dir(), "watching": False, "state": "paused", "last_saved": None,
                "versions": 0, "bytes": 0, "hooks": [], "store": None, "global_store": False})
    if not out["exists"]:
        out["state"] = "missing"
        return out
    try:
        repo = repo_for(entry)
    except (RewindError, OSError):
        out["state"] = "not-set-up"
        return out
    pid = daemon_pid(repo)
    out["watching"] = bool(pid)
    if pid:
        out["state"] = "protected"
    elif entry.get("protected"):
        out["state"] = "starting"
    ids = repo.snapshot_ids()
    out["versions"] = len(ids)
    if ids:
        try:
            out["last_saved"] = repo.get_snapshot(ids[-1])["time"]
        except RewindError:
            pass
    out["bytes"] = store_bytes(repo)
    out["hooks"] = [a for a in AGENTS if installed(repo.root, a)]
    out["store"] = str(repo.store)
    out["global_store"] = repo.is_global
    return out


# ----------------------------------------------------------------- settings
def settings():
    data = read_json(_home_dir() / "settings.json", {}) or {}
    out = dict(SETTINGS_DEFAULTS)
    out.update({k: v for k, v in data.items() if k in SETTINGS_DEFAULTS})
    return out


def save_settings(**changes):
    cur = read_json(_home_dir() / "settings.json", {}) or {}
    for k, v in changes.items():
        if k not in SETTINGS_DEFAULTS:
            raise RewindError("unknown setting %r" % k)
        if not isinstance(v, type(SETTINGS_DEFAULTS[k])):
            raise RewindError("%s expects %s" % (k, type(SETTINGS_DEFAULTS[k]).__name__))
        cur[k] = v
    _home_dir().mkdir(parents=True, exist_ok=True)
    write_json(_home_dir() / "settings.json", cur, indent=2)
    return settings()


# ----------------------------------------------------------------- activity
def log(kind, title, folder=None, detail=None, level="info", **extra):
    """Append an event to the activity feed. Never raises."""
    event = {"time": time.time(), "kind": kind, "title": title, "level": level}
    if folder is not None:
        if isinstance(folder, dict):
            event["folder"] = folder.get("id")
            event["folder_name"] = folder.get("name")
        else:
            p = _norm(folder)
            event["folder"] = folder_id(p)
            event["folder_name"] = Path(p).name
    if detail:
        event["detail"] = str(detail)[:500]
    event.update(extra)
    try:
        path = _home_dir() / "activity.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(path), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True) + "\n")
        if path.stat().st_size > ACTIVITY_MAX_BYTES:
            _trim(path)
    except OSError:
        pass
    return event


def _trim(path):
    try:
        lines = path.read_text("utf-8").splitlines()[-ACTIVITY_KEEP:]
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("\n".join(lines) + "\n", "utf-8")
        os.replace(str(tmp), str(path))
    except OSError:
        pass


def activity(limit=50, folder=None):
    path = _home_dir() / "activity.jsonl"
    try:
        lines = path.read_text("utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if folder and ev.get("folder") != folder:
            continue
        out.append(ev)
        if len(out) >= limit:
            break
    return out
