"""`rewind ui`: a local, token-protected web UI served with the stdlib."""

import json
import secrets
import socketserver
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, restore
from .diff import diff_manifests, rows, summarize
from .scan import scan
from .store import RewindError

STATIC = Path(__file__).with_name("static") / "ui.html"
FONT_DIR = Path(__file__).with_name("static") / "fonts"
# Fonts are versioned with the package, so the browser may keep them for a year.
FONT_CACHE = "public, max-age=31536000, immutable"


def load_fonts():
    """The bundled .woff2 files (Geist, SIL OFL 1.1), keyed by file name."""
    try:
        return {p.name: p.read_bytes() for p in FONT_DIR.glob("*.woff2")}
    except OSError:
        return {}


def _change_json(c):
    out = {"status": c["status"], "path": c["path"]}
    if c.get("old_path"):
        out["old_path"] = c["old_path"]
    for side in ("old", "new"):
        e = c.get(side)
        if e:
            out[side + "_size"] = e.get("s")
            out[side + "_link"] = e.get("l")
    return out


class Api:
    def __init__(self, repo):
        self.repo = repo

    def timeline(self):
        snaps = []
        for m in self.repo.list_snapshots():
            m = dict(m)
            m.pop("changes", None)
            snaps.append(m)
        return {
            "project": str(self.repo.root),
            "name": self.repo.root.name,
            "version": __version__,
            "snapshots": snaps,
            "sessions": self.repo.list_sessions(),
        }

    def _pair(self, sid, base):
        sid = self.repo.resolve(sid)
        meta = self.repo.get_snapshot(sid)
        new = self.repo.snapshot_files(sid)
        if base == "now":
            # what restoring this snapshot would change: current files -> snapshot
            return scan(self.repo).files, new, meta
        parent = meta.get("parent")
        old = self.repo.snapshot_files(parent) if parent is not None and self.repo.snap_path(parent).exists() else {}
        return old, new, meta

    def snapshot(self, sid, base="parent"):
        old, new, meta = self._pair(sid, base)
        changes = diff_manifests(old, new)
        meta = dict(meta)
        meta.pop("changes", None)
        return {"meta": meta, "base": base, "stats": summarize(changes), "changes": [_change_json(c) for c in changes]}

    def diff(self, sid, path, base="parent"):
        old, new, meta = self._pair(sid, base)
        for c in diff_manifests(old, new):
            if c["path"] == path:
                data = rows(self.repo, c)
                data.update(_change_json(c))
                return data
        raise RewindError("%s did not change in #%s" % (path, sid))

    def restore(self, sid, paths):
        res = restore.restore(self.repo, sid, paths or None)
        return self._result(res)

    def undo(self):
        return self._result(restore.undo(self.repo))

    def _result(self, res):
        return {
            "changed": len(res.plan.writes) + len(res.plan.deletes),
            "safety_id": res.safety_id,
            "post_id": res.post_id,
            "errors": [{"path": p, "error": e} for p, e in res.errors],
        }


def make_handler(api, token, port_holder):
    html = STATIC.read_bytes()
    fonts = load_fonts()

    class Handler(BaseHTTPRequestHandler):
        server_version = "rewind/" + __version__

        def log_message(self, fmt, *args):  # quiet
            pass

        def _host_ok(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _send(self, code, body, ctype="application/json", cache=None):
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache or "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src data:; font-src 'self'")
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self, query):
            supplied = self.headers.get("X-Rewind-Token") or (query.get("t") or [""])[0]
            return secrets.compare_digest(supplied, token)

        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            if url.path.startswith("/fonts/"):
                font = fonts.get(url.path[len("/fonts/"):])
                if font is None:
                    return self._send(404, {"error": "not found"})
                return self._send(200, font, "font/woff2", cache=FONT_CACHE)
            if url.path == "/":
                if not self._authorized(q):
                    return self._send(403, b"Forbidden: open the URL printed by `rewind ui`.", "text/plain")
                return self._send(200, html, "text/html; charset=utf-8")
            if not self.headers.get("X-Rewind-Token") or not self._authorized({}):
                return self._send(403, {"error": "missing or bad token"})
            try:
                if url.path == "/api/timeline":
                    return self._send(200, api.timeline())
                if url.path == "/api/snapshot":
                    return self._send(200, api.snapshot(q["id"][0], (q.get("base") or ["parent"])[0]))
                if url.path == "/api/diff":
                    return self._send(200, api.diff(q["id"][0], q["path"][0], (q.get("base") or ["parent"])[0]))
            except RewindError as exc:
                return self._send(400, {"error": str(exc)})
            except (KeyError, IndexError):
                return self._send(400, {"error": "missing parameter"})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            url = urlparse(self.path)
            if not self._host_ok() or not self.headers.get("X-Rewind-Token") or not self._authorized({}):
                return self._send(403, {"error": "forbidden"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._send(400, {"error": "bad json"})
            try:
                if url.path == "/api/restore":
                    return self._send(200, api.restore(body["id"], body.get("paths")))
                if url.path == "/api/undo":
                    return self._send(200, api.undo())
            except RewindError as exc:
                return self._send(400, {"error": str(exc)})
            except KeyError:
                return self._send(400, {"error": "missing parameter"})
            return self._send(404, {"error": "not found"})

    return Handler


class LoopbackServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that does not reverse-resolve 127.0.0.1.

    ``HTTPServer.server_bind`` calls ``socket.getfqdn("127.0.0.1")``. On macOS
    that lookup goes to DNS and can block long enough that ``rewind app`` gives
    up waiting for the server. The name is unused: the server is loopback only.
    """

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def make_server(repo, port=0):
    token = secrets.token_urlsafe(24)
    holder = {}
    server = LoopbackServer(("127.0.0.1", port), make_handler(Api(repo), token, holder))
    server.daemon_threads = True
    url = "http://127.0.0.1:%d/?t=%s" % (server.server_address[1], token)
    return server, url, token


def serve(repo, port=0, open_browser=True):
    server, url, _ = make_server(repo, port)
    print("Rewind UI for %s" % repo.root)
    print("  %s" % url)
    print("Press Ctrl-C to stop.")
    sys.stdout.flush()
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
