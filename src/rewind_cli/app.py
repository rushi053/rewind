"""`rewind app`: the Rewind control center, served locally with the stdlib.

One background server per user (``~/.rewind/app.json`` records its port and
pid) on 127.0.0.1. Every API call must carry the user's token, which lives in
``~/.rewind/app-token`` (mode 0600), and a ``Host`` of 127.0.0.1/localhost.

The server also supervises protection: every folder marked protected gets a
background watcher (``rewind watch --daemon``), restarted if it dies.
"""

import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, plain, registry, restore
from .store import NotInitialized, RewindError, _pid_alive, read_json, rewind_home, write_json

STATIC = Path(__file__).with_name("static")
DEFAULT_PORT = 47823
SUPERVISE_EVERY = 15.0
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _path_key(path):
    """Identity for a directory.

    ``os.path.normcase`` does not fold case on macOS, and ``realpath`` keeps
    the spelling you asked for, so ``~/code`` and ``~/Code`` stay two strings
    on a case-insensitive volume. The inode is the same directory. Linux
    keeps ``code`` and ``Code`` apart because they are different inodes;
    a symlink to the same directory still collapses.
    """
    text = os.fspath(path)
    try:
        st = os.stat(text)
        return (st.st_dev, st.st_ino)
    except OSError:
        try:
            text = os.path.realpath(text)
        except OSError:
            text = os.path.abspath(text)
        return ("path", os.path.normcase(text))

GUARD_MODES = ("block", "snapshot", "warn", "off")


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------- token
def token_path():
    return rewind_home() / "app-token"


def get_token():
    p = token_path()
    try:
        tok = p.read_text().strip()
        if len(tok) >= 20:
            return tok
    except OSError:
        pass
    p.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(24)
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(tok)
    return tok


# ----------------------------------------------------------------------- api
class AppApi:
    def __init__(self):
        self.jobs = {}  # folder id -> {"state": "preparing"|"error", "error": str}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.on_quit = None

    # ------------------------------------------------------------- helpers
    def entry(self, fid):
        try:
            return registry.get(fid)
        except RewindError as exc:
            raise ApiError(404, str(exc))

    def repo(self, fid):
        entry = self.entry(fid)
        try:
            return entry, registry.repo_for(entry)
        except (RewindError, OSError) as exc:
            raise ApiError(409, str(exc))

    def folder_status(self, entry):
        st = registry.status(entry)
        job = self.jobs.get(entry["id"])
        if job:
            st["job"] = job
            if job["state"] == "preparing":
                st["state"] = "preparing"
        return st

    # --------------------------------------------------------------- state
    def state(self):
        from . import launchers

        folders = [self.folder_status(e) for e in registry.load()]
        return {
            "version": __version__,
            "platform": sys.platform,
            "home": str(Path.home()),
            "rewind_home": str(rewind_home()),
            "settings": registry.settings(),
            "autostart": launchers.autostart_enabled(),
            "folders": folders,
        }

    # ---------------------------------------------------------- file system
    @staticmethod
    def _looks_like_project(p):
        for marker in (".git", "package.json", "pyproject.toml", "Cargo.toml", "go.mod", ".rewind", "Gemfile", "pom.xml"):
            if (p / marker).exists():
                return True
        return False

    def fs_list(self, path=None, hidden=False):
        p = Path(os.path.expanduser(path)) if path else Path.home()
        if not p.is_absolute():
            raise ApiError(400, "use a full path")
        try:
            p = p.resolve()
            entries = sorted(os.scandir(str(p)), key=lambda e: e.name.lower())
        except FileNotFoundError:
            raise ApiError(404, "That folder doesn't exist.")
        except NotADirectoryError:
            raise ApiError(400, "That's a file, not a folder.")
        except PermissionError:
            raise ApiError(403, "Rewind isn't allowed to open that folder.")
        protected = {f["path"]: f["id"] for f in registry.load()}
        dirs, hidden_count = [], 0
        for e in entries:
            try:
                if not e.is_dir():
                    continue
            except OSError:
                continue
            if e.name.startswith(".") or e.name in ("node_modules", "__pycache__"):
                hidden_count += 1
                if not hidden:
                    continue
            child = Path(e.path)
            dirs.append({"name": e.name, "path": str(child), "project": self._looks_like_project(child),
                         "protected": protected.get(str(child))})
            if len(dirs) >= 500:
                break
        crumbs = []
        cur = p
        while True:
            crumbs.append({"name": cur.name or str(cur), "path": str(cur)})
            if cur.parent == cur:
                break
            cur = cur.parent
        crumbs.reverse()
        return {"path": str(p), "name": p.name or str(p), "parent": str(p.parent) if p.parent != p else None,
                "crumbs": crumbs, "dirs": dirs, "hidden": hidden_count, "check": registry.check_folder(str(p))}

    def suggestions(self):
        home = Path.home()
        places = []
        seen_places = set()
        for label, rel in (("Home", ""), ("Desktop", "Desktop"), ("Documents", "Documents"), ("code", "code"),
                           ("Code", "Code"), ("Projects", "Projects"), ("projects", "projects"), ("dev", "dev"),
                           ("Developer", "Developer"), ("src", "src"), ("repos", "repos"), ("GitHub", "GitHub"),
                           ("workspace", "workspace")):
            p = home / rel if rel else home
            if not p.is_dir():
                continue
            key = _path_key(p)
            if key in seen_places:
                continue
            seen_places.add(key)
            places.append({"label": label if rel else "Home", "path": str(p)})
        protected = {_path_key(f["path"]) for f in registry.load()}
        projects = []
        seen = set()
        for place in places:
            base = Path(place["path"])
            try:
                children = list(os.scandir(str(base)))
            except OSError:
                continue
            for e in children:
                try:
                    if not e.is_dir() or e.name.startswith("."):
                        continue
                except OSError:
                    continue
                child = Path(e.path)
                key = _path_key(child)
                if key in seen or key in protected:
                    continue
                if (child / ".git").exists() or (child / ".rewind").exists():
                    seen.add(key)
                    try:
                        mtime = e.stat().st_mtime
                    except OSError:
                        mtime = 0
                    projects.append({"name": e.name, "path": str(child), "modified": mtime})
        projects.sort(key=lambda x: -x["modified"])
        return {"places": places, "projects": projects[:8]}

    # -------------------------------------------------------------- folders
    def add_folder(self, path, global_store=None, protect=True):
        check = registry.check_folder(path)
        if not check["ok"]:
            raise ApiError(400, check["error"])
        repo = registry.setup_folder(check["path"], global_store=global_store)
        entry = registry.register(check["path"], protected=bool(protect), source="app")
        fid = entry["id"]
        with self.lock:
            self.jobs[fid] = {"state": "preparing", "started": time.time()}

        def work():
            try:
                if not repo.snapshot_ids():
                    repo.capture(message="initial snapshot", trigger="init")
                if protect:
                    registry.set_protected(fid, True)
                else:
                    registry.log("added", "Added %s" % entry["name"], folder=entry)
                with self.lock:
                    self.jobs.pop(fid, None)
            except Exception as exc:  # surface it on the card
                with self.lock:
                    self.jobs[fid] = {"state": "error", "error": str(exc)}

        threading.Thread(target=work, name="rewind-add", daemon=True).start()
        return {"id": fid, "folder": self.folder_status(entry), "warning": check.get("warning")}

    def remove_folder(self, fid, delete_history=False):
        from .watch import stop_daemon

        entry = self.entry(fid)
        store = None
        try:
            repo = registry.repo_for(entry)
            stop_daemon(repo)
            store = repo.store
        except (RewindError, OSError, NotInitialized):
            pass
        registry.unregister(fid)
        deleted = False
        if delete_history and store is not None:
            projects_dir = rewind_home() / "projects"
            local = Path(entry["path"]) / ".rewind"
            if store.resolve() == local.resolve() or projects_dir.resolve() in store.resolve().parents:
                shutil.rmtree(str(store), ignore_errors=True)
                deleted = True
        registry.log("removed", "Stopped protecting %s" % entry["name"], folder=entry,
                     detail="saved history deleted" if deleted else "saved history kept in %s" % store)
        return {"removed": True, "history_deleted": deleted}

    def protect(self, fid, on):
        self.entry(fid)
        try:
            registry.set_protected(fid, bool(on))
        except (RewindError, OSError) as exc:
            raise ApiError(409, str(exc))
        return self.folder_status(self.entry(fid))

    def overview(self, fid):
        entry, repo = self.repo(fid)
        sessions = {s["id"]: s for s in repo.list_sessions()}
        snaps = [m for m in repo.list_snapshots() if m.get("trigger") != "safety"]
        recent = [plain.describe_snapshot(m, sessions) for m in reversed(snaps[-25:])]
        for item in recent:
            item["when"] = plain.when(item["time"])
        last = next((r for r in recent if r["kind"] not in ("blocked",)), None)
        sess = repo.latest_session()
        session = None
        if sess and sess.get("start_snapshot") is not None:
            ids = [i for i in sess.get("snapshots", []) if repo.snap_path(i).exists()]
            changed = 0
            for i in ids:
                try:
                    changed += repo.get_snapshot(i).get("nchanges", 0)
                except RewindError:
                    pass
            session = {"id": sess["id"], "who": plain.agent_name(sess), "started": sess["started"],
                       "when": plain.when(sess["started"]), "ended": sess.get("ended"), "last": sess.get("last") or sess["started"],
                       "start_snapshot": sess["start_snapshot"], "saves": len(ids), "changes": changed}
        try:
            deleted = restore.deleted_files(repo, max_versions=60)
        except RewindError:
            deleted = []
        try:
            redo = bool(restore.redo(repo, dry_run=True).plan)
        except RewindError:
            redo = False
        return {"folder": self.folder_status(entry), "last": last, "recent": recent, "session": session, "redo": redo,
                "deleted_count": len(deleted), "deleted_sample": [d["path"] for d in deleted[:3]]}

    # ----------------------------------------------------- undo / go back
    def goback_points(self, fid):
        _entry, repo = self.repo(fid)
        now = time.time()
        lt = time.localtime(now)
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        snaps = repo.list_snapshots()
        presets = []

        def add(key, label, hint, ts, meta=None):
            meta = meta if meta is not None else restore.snapshot_before(repo, ts)
            presets.append({"key": key, "label": label, "hint": hint, "ts": ts,
                            "snapshot_id": meta["id"] if meta else None,
                            "time": meta["time"] if meta else None,
                            "when": plain.when(meta["time"], now) if meta else None})

        add("5m", "5 minutes ago", "Just before the latest changes", now - 300)
        add("1h", "1 hour ago", None, now - 3600)
        morning = restore.snapshot_before(repo, midnight)
        if morning is None:
            morning = next((m for m in snaps if m["time"] >= midnight), None)
        add("morning", "This morning", "How things were when your day started", midnight, morning)
        add("yesterday", "Yesterday", "This time yesterday", now - 86400)
        oldest = snaps[0] if snaps else None
        for p in presets:
            if p["snapshot_id"] is None:
                p["hint"] = ("Rewind started protecting this folder %s" % plain.when(oldest["time"], now)) if oldest else "Nothing saved yet"
        points = [{"id": m["id"], "time": m["time"]} for m in snaps if m.get("trigger") != "safety"][-1000:]
        return {"presets": presets, "points": points, "now": now}

    def _preview(self, repo, kind, sid=None):
        if kind == "undo":
            try:
                res = restore.undo(repo, dry_run=True)
            except RewindError as exc:
                return {"kind": kind, "nothing": True, "message": "Nothing to undo yet: %s." % str(exc).split(": ", 1)[-1]}
            target = res.plan.target_id
            meta = repo.get_snapshot(target)
            title = "Undo the last change"
        elif kind == "redo":
            try:
                res = restore.redo(repo, dry_run=True)
            except RewindError:
                return {"kind": kind, "nothing": True, "message": "Nothing to redo: redo only works right after an undo."}
            target = res.plan.target_id
            meta = repo.get_snapshot(target)
            title = "Redo"
        elif kind == "back":
            if sid is None:
                raise ApiError(400, "choose a point in time")
            target = repo.resolve(sid)
            res = restore.restore(repo, target, dry_run=True)
            meta = repo.get_snapshot(target)
            title = "Go back to %s" % plain.when(meta["time"])
        elif kind == "session":
            sess, res = restore.undo_session(repo, sid, dry_run=True)
            target = sess["start_snapshot"]
            meta = repo.get_snapshot(target)
            title = "Undo the %s session" % (plain.agent_name(sess) or "AI")
        else:
            raise ApiError(400, "unknown preview kind %r" % kind)
        summary = plain.describe_plan(res.plan)
        return {"kind": kind, "target_id": target, "time": meta["time"], "when": plain.when(meta["time"]),
                "title": title, "summary": summary, "nothing": not res.plan, "session": sid if kind == "session" else None}

    def preview(self, fid, kind, sid=None):
        _entry, repo = self.repo(fid)
        return self._preview(repo, kind, sid)

    def apply(self, fid, kind, sid=None, expect=None):
        entry, repo = self.repo(fid)
        with repo.lock:
            prev = self._preview(repo, kind, sid)
            if prev.get("nothing"):
                return {"changed": 0, "message": "Nothing to change: your files already match.", "preview": prev}
            if expect is not None and prev["target_id"] != expect:
                raise ApiError(409, "Your files changed since the preview. Take another look before confirming.")
            if kind == "undo":
                res = restore.undo(repo)
                what = "Undid the last change in %s" % entry["name"]
            elif kind == "redo":
                res = restore.redo(repo)
                what = "Redid the change in %s" % entry["name"]
            elif kind == "back":
                res = restore.restore(repo, prev["target_id"])
                what = "Went back to %s in %s" % (prev["when"], entry["name"])
            else:
                _sess, res = restore.undo_session(repo, sid)
                what = "Undid an AI session in %s" % entry["name"]
        registry.log("restored", what, folder=entry, detail=prev["summary"]["sentence"])
        return {"changed": len(res.plan.writes) + len(res.plan.deletes) - len(res.errors), "safety_id": res.safety_id,
                "errors": [{"path": p, "error": e} for p, e in res.errors], "summary": prev["summary"],
                "message": what, "kind": kind}

    def deleted(self, fid, include_git=False):
        _entry, repo = self.repo(fid)
        sessions = {s["id"]: s for s in repo.list_sessions()}
        items = restore.deleted_files(repo, include_git=include_git)
        for it in items:
            it["who"] = plain.agent_name(sessions.get(it["session"])) if it.get("session") else None
            it["when"] = plain.when(it["deleted_at"]) if it.get("deleted_at") else "since the last save"
        return {"files": items[:2000], "total": len(items)}

    def recover(self, fid, paths):
        entry, repo = self.repo(fid)
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
            raise ApiError(400, "choose at least one file")
        res = restore.recover_paths(repo, paths)
        n = len(res.plan.writes) - len(res.errors)
        registry.log("recovered", "Brought back %d deleted file%s in %s" % (n, "" if n == 1 else "s", entry["name"]),
                     folder=entry, detail=", ".join(sorted(p for p, _e, _k in res.plan.writes)[:10]))
        return {"recovered": [p for p, _e, _k in res.plan.writes], "errors": [{"path": p, "error": e} for p, e in res.errors],
                "safety_id": res.safety_id}

    # ------------------------------------------------------------- settings
    @staticmethod
    def _folder_settings(repo):
        cfg = repo.config
        return {"guard_mode": cfg.get("guard.mode"), "keep_days": cfg.get("gc.daily_days"),
                "keep_all_hours": cfg.get("gc.keep_all_hours"), "hourly_days": cfg.get("gc.hourly_days"),
                "max_file_size_mb": cfg.get("max_file_size_mb"), "config_path": str(cfg.path),
                "ignore_path": str(repo.root / ".rewindignore"), "store": str(repo.store),
                "bytes": registry.store_bytes(repo, ttl=0), "versions": len(repo.snapshot_ids())}

    def folder_settings(self, fid):
        _entry, repo = self.repo(fid)
        return self._folder_settings(repo)

    @staticmethod
    def _apply_settings(repo, body):
        from .config import ConfigError

        try:
            if "guard_mode" in body:
                if body["guard_mode"] not in GUARD_MODES:
                    raise ApiError(400, "unknown safety mode")
                repo.config.set("guard.mode", json.dumps(body["guard_mode"]))
            if "keep_days" in body:
                days = body["keep_days"]
                if isinstance(days, bool) or not isinstance(days, (int, float)) or not (1 <= days <= 3650):
                    raise ApiError(400, "keep_days must be between 1 and 3650")
                repo.config.set("gc.daily_days", json.dumps(days))
                if float(repo.config.get("gc.hourly_days")) > days:
                    repo.config.set("gc.hourly_days", json.dumps(days))
        except ConfigError as exc:
            raise ApiError(400, str(exc))

    def save_folder_settings(self, fid, body):
        _entry, repo = self.repo(fid)
        self._apply_settings(repo, body)
        repo.reload_config()
        return self._folder_settings(repo)

    def save_all_settings(self, body):
        done = []
        for entry in registry.load():
            try:
                repo = registry.repo_for(entry)
            except (RewindError, OSError):
                continue
            self._apply_settings(repo, body)
            done.append(entry["id"])
        return {"updated": done}

    def cleanup(self, fid, dry_run=False):
        from .gc import run_gc

        entry, repo = self.repo(fid)
        res = run_gc(repo, dry_run=bool(dry_run))
        registry._SIZE_CACHE.pop(str(repo.store), None)
        if not dry_run and (res["snapshots_deleted"] or res["objects_deleted"]):
            registry.log("cleanup", "Cleaned up %s in %s" % (_size(res["bytes_freed"]), entry["name"]), folder=entry)
        res.pop("deleted_ids", None)
        res["bytes_now"] = registry.store_bytes(repo, ttl=0)
        return res

    def save_settings(self, body):
        allowed = {k: v for k, v in body.items() if k in registry.SETTINGS_DEFAULTS}
        try:
            return registry.save_settings(**allowed)
        except RewindError as exc:
            raise ApiError(400, str(exc))

    def autostart(self, on):
        from . import launchers

        res = launchers.set_autostart(bool(on))
        registry.log("settings", "Start at login turned %s" % ("on" if res["enabled"] else "off"))
        return res

    # ---------------------------------------------------------------- tools
    def tools(self, fid=None):
        from . import integrations

        folder = self.entry(fid)["path"] if fid else None
        return {"tools": integrations.overview(folder), "folder": folder}

    def _plan(self, body):
        from . import integrations

        scope = body.get("scope") or "global"
        folder = self.entry(body["f"])["path"] if scope == "folder" else None
        try:
            return integrations.plan(body["tool"], body["kind"], scope, folder, body.get("action") or "connect")
        except KeyError as exc:
            raise ApiError(400, "missing %s" % exc)

    def tool_plan(self, body):
        return self._plan(body).to_json()

    def tool_apply(self, body):
        from . import integrations

        p = self._plan(body)
        res = integrations.apply(p)
        title = next(t["title"] for t in integrations.TOOLS if t["id"] == p.tool_id)
        what = "Safety hooks" if p.kind == "hooks" else "Rewind tools (MCP)"
        verb = "Connected" if p.action == "connect" else "Disconnected"
        registry.log("connected" if p.action == "connect" else "disconnected", "%s %s: %s" % (verb, title, what),
                     detail=str(p.path) + ((" (backup: %s)" % res["backup"]) if res.get("backup") else ""),
                     folder=p.folder)
        return res

    def quit(self):
        if self.on_quit:
            threading.Timer(0.2, self.on_quit).start()
        return {"quitting": True}

    # ------------------------------------------------------------ supervisor
    def supervise(self):
        while not self.stop_event.is_set():
            try:
                registry.ensure_watchers()
            except Exception as exc:
                sys.stderr.write("rewind app: supervisor: %s\n" % exc)
            self.stop_event.wait(SUPERVISE_EVERY)


def _size(n):
    from .term import size

    return size(n)


# ------------------------------------------------------------------- server
_FOLDER_ROUTE = re.compile(r"^/api/f/([0-9a-f]{16})/([a-z-]+)$")


def make_handler(api, token):
    app_html = (STATIC / "app.html").read_bytes()
    ui_html = (STATIC / "ui.html").read_bytes()
    icon = (STATIC / "icon-32.png").read_bytes()
    from .ui import FONT_CACHE, load_fonts
    fonts = load_fonts()
    from .ui import Api as TimelineApi

    class Handler(BaseHTTPRequestHandler):
        server_version = "rewind/" + __version__

        def log_message(self, fmt, *args):
            pass

        def _host_ok(self):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in ("127.0.0.1", "localhost")

        def _origin_ok(self):
            origin = self.headers.get("Origin")
            if not origin:
                return True
            return bool(re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?$", origin))

        def _send(self, code, body, ctype="application/json", frame=False, cache=None):
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache or "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                             "connect-src 'self'; img-src 'self' data:; font-src 'self'; frame-src 'self'; frame-ancestors %s"
                             % ("'self'" if frame else "'none'"))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self, query=None):
            supplied = self.headers.get("X-Rewind-Token") or ((query or {}).get("t") or [""])[0]
            return bool(supplied) and secrets.compare_digest(supplied, token)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > 1_000_000:
                raise ApiError(413, "request too large")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                raise ApiError(400, "bad json")
            if not isinstance(body, dict):
                raise ApiError(400, "expected a JSON object")
            return body

        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            if url.path == "/favicon.png":
                return self._send(200, icon, "image/png")
            if url.path.startswith("/fonts/"):
                font = fonts.get(url.path[len("/fonts/"):])
                if font is None:
                    return self._send(404, {"error": "not found"})
                return self._send(200, font, "font/woff2", cache=FONT_CACHE)
            if url.path in ("/", "/ui"):
                if not self._authorized(q):
                    return self._send(403, b"Forbidden. Open Rewind with the `rewind app` command or its launcher.", "text/plain")
                if url.path == "/ui":
                    return self._send(200, ui_html, "text/html; charset=utf-8", frame=True)
                return self._send(200, app_html, "text/html; charset=utf-8")
            if not self.headers.get("X-Rewind-Token") or not self._authorized():
                return self._send(403, {"error": "missing or bad token"})
            self._route("GET", url.path, q, {})

        def do_POST(self):
            url = urlparse(self.path)
            if not self._host_ok() or not self._origin_ok() or not self.headers.get("X-Rewind-Token") or not self._authorized():
                return self._send(403, {"error": "forbidden"})
            try:
                body = self._body()
            except ApiError as exc:
                return self._send(exc.code, {"error": str(exc)})
            self._route("POST", url.path, {}, body)

        def _route(self, method, path, q, body):
            one = lambda k, d=None: (q.get(k) or [d])[0]
            try:
                result = None
                m = _FOLDER_ROUTE.match(path)
                if m:
                    result = self._folder(method, m.group(1), m.group(2), q, body, one)
                elif method == "GET":
                    if path == "/api/ping":
                        result = {"app": "rewind", "version": __version__, "pid": os.getpid()}
                    elif path == "/api/state":
                        result = api.state()
                    elif path == "/api/fs":
                        result = api.fs_list(one("path"), one("hidden") == "1")
                    elif path == "/api/fs/suggestions":
                        result = api.suggestions()
                    elif path == "/api/fs/check":
                        result = registry.check_folder(one("path", ""))
                    elif path == "/api/activity":
                        result = {"events": registry.activity(int(one("limit", "60")), one("f"))}
                    elif path == "/api/tools":
                        result = api.tools(one("f"))
                else:
                    if path == "/api/folders/add":
                        result = api.add_folder(body.get("path", ""), body.get("global_store", None), body.get("protect", True))
                    elif path == "/api/folders/remove":
                        result = api.remove_folder(body["id"], bool(body.get("delete_history")))
                    elif path == "/api/settings":
                        result = api.save_settings(body)
                    elif path == "/api/settings/folders":
                        result = api.save_all_settings(body)
                    elif path == "/api/tools/plan":
                        result = api.tool_plan(body)
                    elif path == "/api/tools/apply":
                        result = api.tool_apply(body)
                    elif path == "/api/autostart":
                        result = api.autostart(bool(body.get("on")))
                    elif path == "/api/quit":
                        result = api.quit()
                if result is None:
                    return self._send(404, {"error": "not found"})
                return self._send(200, result)
            except ApiError as exc:
                return self._send(exc.code, {"error": str(exc)})
            except RewindError as exc:
                return self._send(400, {"error": str(exc)})
            except (KeyError, IndexError, ValueError, TypeError) as exc:
                return self._send(400, {"error": "bad request: %s" % exc})
            except Exception as exc:
                sys.stderr.write("rewind app: %s %s: %r\n" % (method, path, exc))
                return self._send(500, {"error": "unexpected error: %s" % exc})

        def _folder(self, method, fid, action, q, body, one):
            if method == "GET":
                if action == "overview":
                    return api.overview(fid)
                if action == "goback":
                    return api.goback_points(fid)
                if action == "deleted":
                    return api.deleted(fid, one("git") == "1")
                if action == "settings":
                    return api.folder_settings(fid)
                if action in ("timeline", "snapshot", "diff"):
                    _e, repo = api.repo(fid)
                    t = TimelineApi(repo)
                    if action == "timeline":
                        return t.timeline()
                    if action == "snapshot":
                        return t.snapshot(q["id"][0], one("base", "parent"))
                    return t.diff(q["id"][0], q["path"][0], one("base", "parent"))
                return None
            if action == "protect":
                return api.protect(fid, bool(body.get("on")))
            if action == "preview":
                return api.preview(fid, body.get("kind"), body.get("snapshot_id") if body.get("kind") != "session" else body.get("session"))
            if action == "apply":
                sid = body.get("snapshot_id") if body.get("kind") != "session" else body.get("session")
                return api.apply(fid, body.get("kind"), sid, body.get("expect"))
            if action == "recover":
                return api.recover(fid, body.get("paths"))
            if action == "settings":
                return api.save_folder_settings(fid, body)
            if action == "cleanup":
                return api.cleanup(fid, body.get("dry_run", False))
            if action in ("restore", "undo"):  # the embedded timeline view
                entry, repo = api.repo(fid)
                t = TimelineApi(repo)
                res = t.restore(body["id"], body.get("paths")) if action == "restore" else t.undo()
                registry.log("restored", ("Restored from the timeline in %s" if action == "restore" else "Undid the last change in %s")
                             % entry["name"], folder=entry)
                return res
            return None

    return Handler


def make_server(port=0, token=None):
    """Create (but don't start) the app server. Returns (server, url, token, api)."""
    token = token or get_token()
    api = AppApi()
    from .ui import LoopbackServer

    server = LoopbackServer(("127.0.0.1", port), make_handler(api, token))
    server.daemon_threads = True
    url = "http://127.0.0.1:%d/?t=%s" % (server.server_address[1], token)
    api.on_quit = server.shutdown
    return server, url, token, api


# ---------------------------------------------------------------- lifecycle
def info_path():
    return rewind_home() / "app.json"


def running():
    """Info about the running app server, or None."""
    info = read_json(info_path())
    if not info or not _pid_alive(int(info.get("pid") or 0)):
        return None
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/api/ping" % info["port"])
        req.add_header("X-Rewind-Token", get_token())
        with _OPENER.open(req, timeout=2) as r:
            data = json.loads(r.read())
        if data.get("app") == "rewind":
            info["url"] = "http://127.0.0.1:%d/?t=%s" % (info["port"], get_token())
            return info
    except (OSError, ValueError, KeyError):
        pass
    return None


def _bind(port):
    candidates = [port] if port else []
    if not port:
        env = os.environ.get("REWIND_APP_PORT")
        candidates = ([int(env)] if env and env.isdigit() else [DEFAULT_PORT]) + [0]
    last = None
    for p in candidates:
        try:
            return make_server(p)
        except OSError as exc:
            last = exc
    raise RewindError("could not start the Rewind app server: %s" % last)


def serve_foreground(port=0, open_browser=False, supervise=True):
    existing = running()
    if existing:
        print("Rewind is already running: %s" % existing["url"])
        if open_browser:
            webbrowser.open(existing["url"])
        return 0
    server, url, _token, api = _bind(port)
    rewind_home().mkdir(parents=True, exist_ok=True)
    write_json(info_path(), {"pid": os.getpid(), "port": server.server_address[1], "started": time.time(),
                             "version": __version__}, indent=2)
    if supervise:
        threading.Thread(target=api.supervise, name="rewind-supervisor", daemon=True).start()

    def _term(*_a):
        threading.Thread(target=server.shutdown, daemon=True).start()

    try:
        signal.signal(signal.SIGTERM, _term)
    except (ValueError, AttributeError):
        pass
    print("Rewind app running at %s" % url)
    print("Press Ctrl-C to stop (folder protection keeps running in the background).")
    sys.stdout.flush()
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        api.stop_event.set()
        server.server_close()
        info = read_json(info_path())
        if info and info.get("pid") == os.getpid():
            try:
                info_path().unlink()
            except OSError:
                pass
    return 0


def _log_tail(path, limit=40):
    try:
        lines = Path(path).read_text(errors="replace").splitlines()
    except OSError as exc:
        return "(could not read %s: %s)" % (path, exc)
    if not lines:
        return "(app.log is empty)"
    return "\n".join(lines[-limit:])


def start_background(port=0, timeout=15.0):
    """Start the app server as a detached process and wait until it answers."""
    rewind_home().mkdir(parents=True, exist_ok=True)
    log_path = rewind_home() / "app.log"
    log = open(str(log_path), "ab")
    # -u so a crash traceback reaches app.log instead of sitting in a block buffer.
    cmd = [sys.executable, "-u", "-m", "rewind_cli", "app", "--foreground", "--no-open"]
    if port:
        cmd += ["--port", str(port)]
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=str(Path.home()))
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    finally:
        log.close()
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = running()
        if info:
            return info
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    state = "still running, pid %d" % proc.pid if proc.poll() is None else "exited %s" % proc.returncode
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
    raise RewindError("the Rewind app did not start (%s).\n--- %s ---\n%s" % (state, log_path, _log_tail(log_path)))


def ensure_running(port=0):
    return running() or start_background(port)


def stop(timeout=5.0):
    info = read_json(info_path())
    if not info:
        return None
    pid = int(info.get("pid") or 0)
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.time() + timeout
        while time.time() < deadline and _pid_alive(pid):
            time.sleep(0.05)
            try:
                os.waitpid(pid, os.WNOHANG)
            except (OSError, AttributeError):
                pass
    try:
        info_path().unlink()
    except OSError:
        pass
    return pid
