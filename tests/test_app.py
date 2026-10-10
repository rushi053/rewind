import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from conftest import read, run_rewind, write
from rewind_cli import app, registry
from rewind_cli.store import Repo, rewind_home

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Client:
    def __init__(self, base, token):
        self.base, self.token = base, token

    def req(self, method, path, body=None, headers=None, token=True):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method)
        if token:
            r.add_header("X-Rewind-Token", self.token)
        if data is not None:
            r.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            r.add_header(k, v)
        try:
            with OPENER.open(r, timeout=20) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if resp.headers.get("Content-Type", "").startswith("application/json") else raw), resp.headers
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw), e.headers
            except ValueError:
                return e.code, raw, e.headers

    def get(self, path, **kw):
        return self.req("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.req("POST", path, body if body is not None else {}, **kw)


@pytest.fixture
def client(fake_home, stop_watchers):
    server, url, token, api = app.make_server(0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield Client(url.split("/?")[0], token)
    server.shutdown()
    server.server_close()


def make_project(fake_home, name="site", files=None):
    root = fake_home / "code" / name
    root.mkdir(parents=True)
    for rel, text in (files or {"index.html": "<h1>v1</h1>\n", "app.js": "console.log(1)\n", ".env": "KEY=1\n"}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def add_and_wait(client, root, protect=True):
    code, res, _ = client.post("/api/folders/add", {"path": str(root), "protect": protect})
    assert code == 200, res
    fid = res["id"]
    deadline = time.time() + 20
    while time.time() < deadline:
        st = next(f for f in client.get("/api/state")[1]["folders"] if f["id"] == fid)
        if st["state"] != "preparing":
            return fid, st
        time.sleep(0.1)
    raise AssertionError("folder never finished preparing")


# ---------------------------------------------------------------- security
def test_pages_and_api_require_token_and_local_host(client):
    code, body, _ = client.get("/", token=False)
    assert code == 403
    code, body, headers = client.get("/?t=" + client.token, token=False)
    assert code == 200 and b"<title>Rewind</title>" in body and b"https://" not in body.split(b"<script>")[0]
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    code, body, headers = client.get("/ui?f=abc&embed=1&t=" + client.token, token=False)
    assert code == 200 and "frame-ancestors 'self'" in headers["Content-Security-Policy"]
    assert client.get("/favicon.png", token=False)[0] == 200
    code, body, headers = client.get("/fonts/Geist-Variable.woff2", token=False)
    assert code == 200 and body[:4] == b"wOF2" and headers["Content-Type"] == "font/woff2"
    assert "immutable" in headers["Cache-Control"]
    assert client.get("/fonts/GeistMono-Variable.woff2", token=False)[0] == 200
    assert client.get("/fonts/../app.py", token=False)[0] == 404
    assert "font-src 'self'" in client.get("/?t=" + client.token, token=False)[2]["Content-Security-Policy"]
    assert client.get("/api/state", token=False)[0] == 403
    assert client.get("/api/state?t=" + client.token, token=False)[0] == 403  # query token is not enough for the API
    assert client.get("/api/state", headers={"Host": "evil.example:80"})[0] == 403
    assert client.post("/api/settings", {"onboarded": True}, headers={"Origin": "https://evil.example"})[0] == 403
    assert client.post("/api/settings", {"onboarded": True}, headers={"Origin": "http://127.0.0.1:1234"})[0] == 200
    assert client.get("/api/nope")[0] == 404
    assert client.get("/api/f/0123456789abcdef/overview")[0] == 404


def test_token_is_stable_and_private(fake_home):
    tok = app.get_token()
    assert tok == app.get_token() and len(tok) >= 20
    if os.name != "nt":
        assert oct(app.token_path().stat().st_mode & 0o777) == "0o600"


# ---------------------------------------------------------- file browser
def test_file_browser_and_suggestions(client, fake_home):
    make_project(fake_home, "shop")
    subprocess.run(["git", "init", "-q", str(fake_home / "code" / "shop")], check=True)
    (fake_home / "code" / ".hidden").mkdir()
    (fake_home / "Desktop").mkdir()
    code, d, _ = client.get("/api/fs?path=" + str(fake_home / "code"))
    assert code == 200 and [x["name"] for x in d["dirs"]] == ["shop"] and d["hidden"] == 1
    assert d["dirs"][0]["project"] is True and d["crumbs"][-1]["name"] == "code" and d["check"]["ok"]
    d = client.get("/api/fs?hidden=1&path=" + str(fake_home / "code"))[1]
    assert [x["name"] for x in d["dirs"]] == [".hidden", "shop"]
    assert client.get("/api/fs")[1]["path"] == str(fake_home)
    assert client.get("/api/fs?path=" + str(fake_home / "missing"))[0] == 404
    assert client.get("/api/fs?path=relative")[0] == 400
    s = client.get("/api/fs/suggestions")[1]
    labels = [p["label"] for p in s["places"]]
    assert labels[:2] == ["Home", "Desktop"] and "code" in labels
    assert [p["name"] for p in s["projects"]] == ["shop"]
    assert client.get("/api/fs/check?path=" + str(fake_home))[1]["error"].startswith("Your whole home folder")


# ------------------------------------------------------------- folders
def test_add_protect_pause_remove(client, fake_home):
    root = make_project(fake_home)
    code, res, _ = client.post("/api/folders/add", {"path": str(fake_home)})
    assert code == 400 and "home folder" in res["error"]
    fid, st = add_and_wait(client, root)
    assert st["state"] == "protected" and st["versions"] == 1 and st["protected"], st
    assert (root / ".rewind").is_dir() and (root / ".rewindignore").exists()
    assert client.post("/api/folders/add", {"path": str(root)})[0] == 400  # already there
    code, st, _ = client.post("/api/f/%s/protect" % fid, {"on": False})
    assert st["state"] == "paused" and not st["watching"]
    code, st, _ = client.post("/api/f/%s/protect" % fid, {"on": True})
    assert st["state"] == "protected"
    code, res, _ = client.post("/api/folders/remove", {"id": fid, "delete_history": True})
    assert res == {"removed": True, "history_deleted": True}
    assert not (root / ".rewind").exists() and (root / "index.html").exists()
    assert client.get("/api/state")[1]["folders"] == []
    kinds = [e["kind"] for e in client.get("/api/activity")[1]["events"]]
    assert kinds[0] == "removed" and "protected" in kinds and "paused" in kinds


def test_add_with_global_store_and_keep_history_on_remove(client, fake_home):
    root = make_project(fake_home)
    fid, st = add_and_wait(client, root, protect=False)
    assert st["state"] == "paused"
    other = make_project(fake_home, "other")
    code, res, _ = client.post("/api/folders/add", {"path": str(other), "global_store": True, "protect": False})
    assert code == 200 and not (other / ".rewind").exists()
    client.post("/api/folders/remove", {"id": fid})
    assert (root / ".rewind").exists()  # history kept unless asked


def test_overview_preview_apply_and_conflict(client, fake_home):
    root = make_project(fake_home)
    fid, _ = add_and_wait(client, root, protect=False)
    repo = Repo.find(root)
    (root / "index.html").write_text("<h1>broken by an agent</h1>\n")
    (root / "app.js").unlink()
    (root / "new.txt").write_text("created later\n")
    repo.capture(message="after Edit", trigger="hook", tool="Edit")
    o = client.get("/api/f/%s/overview" % fid)[1]
    assert o["last"]["summary"] == "1 file edited, 1 file added, 1 file deleted"
    assert o["deleted_count"] == 1 and o["deleted_sample"] == ["app.js"]
    code, p, _ = client.post("/api/f/%s/preview" % fid, {"kind": "undo"})
    assert code == 200 and p["summary"]["sentence"] == "1 file will come back, 1 file will change back and 1 file will be removed."
    assert p["summary"]["come_back"] == ["app.js"] and p["summary"]["removed"] == ["new.txt"]
    code, res, _ = client.post("/api/f/%s/apply" % fid, {"kind": "undo", "expect": p["target_id"] + 99})
    assert code == 409 and "changed since the preview" in res["error"]
    assert (root / "new.txt").exists()
    code, res, _ = client.post("/api/f/%s/apply" % fid, {"kind": "undo", "expect": p["target_id"]})
    assert code == 200 and res["changed"] == 3
    assert read_text(root / "index.html") == "<h1>v1</h1>\n" and (root / "app.js").exists() and not (root / "new.txt").exists()
    assert client.get("/api/activity")[1]["events"][0]["kind"] == "restored"
    code, p, _ = client.post("/api/f/%s/preview" % fid, {"kind": "nonsense"})
    assert code == 400


def read_text(p):
    return p.read_text()


def test_deleted_files_and_recover(client, fake_home):
    root = make_project(fake_home, files={"a.txt": "a", "app/[id].tsx": "page", "keep.txt": "k"})
    fid, _ = add_and_wait(client, root, protect=False)
    repo = Repo.find(root)
    (root / "a.txt").unlink()
    (root / "app" / "[id].tsx").unlink()
    repo.capture()
    (root / "keep.txt").unlink()  # deleted after the latest save
    d = client.get("/api/f/%s/deleted" % fid)[1]
    paths = [f["path"] for f in d["files"]]
    assert paths[0] == "keep.txt" and set(paths) == {"a.txt", "app/[id].tsx", "keep.txt"}
    assert d["files"][0]["when"] == "since the last save" and d["files"][1]["deleted_at"]
    git = client.get("/api/f/%s/deleted?git=1" % fid)[1]
    assert git["total"] >= 3
    code, res, _ = client.post("/api/f/%s/recover" % fid, {"paths": ["app/[id].tsx"]})
    assert code == 200 and res["recovered"] == ["app/[id].tsx"]
    assert (root / "app" / "[id].tsx").read_text() == "page" and not (root / "a.txt").exists()
    assert client.post("/api/f/%s/recover" % fid, {"paths": []})[0] == 400
    assert "Brought back" in client.get("/api/activity")[1]["events"][0]["title"]


def test_folder_settings_cleanup_and_all_folders(client, fake_home):
    a = make_project(fake_home, "a")
    b = make_project(fake_home, "b")
    fa, _ = add_and_wait(client, a, protect=False)
    fb, _ = add_and_wait(client, b, protect=False)
    s = client.get("/api/f/%s/settings" % fa)[1]
    assert s["guard_mode"] == "block" and s["keep_days"] == 30 and s["versions"] == 1
    code, s, _ = client.post("/api/f/%s/settings" % fa, {"guard_mode": "warn", "keep_days": 5})
    assert code == 200 and s["guard_mode"] == "warn" and s["keep_days"] == 5 and s["hourly_days"] == 5
    assert Repo.find(a).config.get("guard.mode") == "warn"
    assert client.post("/api/f/%s/settings" % fa, {"guard_mode": "yolo"})[0] == 400
    assert client.post("/api/f/%s/settings" % fa, {"keep_days": 0})[0] == 400
    code, res, _ = client.post("/api/settings/folders", {"guard_mode": "off"})
    assert set(res["updated"]) == {fa, fb}
    assert Repo.find(b).config.get("guard.mode") == "off"
    res = client.post("/api/f/%s/cleanup" % fa, {"dry_run": True})[1]
    assert res["snapshots_deleted"] == 0 and res["dry_run"] is True and res["bytes_now"] > 0
    code, st, _ = client.post("/api/settings", {"notifications": False, "unknown": 1})
    assert st == {"notifications": False, "onboarded": False}
    assert client.post("/api/settings", {"notifications": "nope"})[0] == 400


def test_tools_endpoints(client, fake_home):
    root = make_project(fake_home)
    fid, _ = add_and_wait(client, root, protect=False)
    t = client.get("/api/tools?f=" + fid)[1]
    assert t["folder"] == str(root) and {x["id"] for x in t["tools"]} >= {"claude-code", "cursor", "vscode"}
    code, p, _ = client.post("/api/tools/plan", {"tool": "cursor", "kind": "mcp", "scope": "folder", "f": fid})
    assert code == 200 and p["path"] == str(root / ".cursor" / "mcp.json") and not p["exists"]
    assert any('"rewind"' in line for line in p["diff"])
    code, res, _ = client.post("/api/tools/apply", {"tool": "cursor", "kind": "mcp", "scope": "folder", "f": fid})
    assert code == 200 and (root / ".cursor" / "mcp.json").exists()
    code, res, _ = client.post("/api/tools/apply", {"tool": "claude-code", "kind": "hooks", "scope": "global"})
    assert code == 200 and (fake_home / ".claude" / "settings.json").exists()
    ev = client.get("/api/activity")[1]["events"][0]
    assert ev["kind"] == "connected" and "Claude Code" in ev["title"]
    assert client.post("/api/tools/plan", {"tool": "windsurf", "kind": "hooks", "scope": "global"})[0] == 400
    assert client.post("/api/tools/plan", {"kind": "mcp"})[0] == 400


def test_embedded_timeline_endpoints(client, fake_home):
    root = make_project(fake_home)
    fid, _ = add_and_wait(client, root, protect=False)
    repo = Repo.find(root)
    (root / "app.js").write_text("console.log(2)\n")
    repo.capture()
    tl = client.get("/api/f/%s/timeline" % fid)[1]
    assert [s["id"] for s in tl["snapshots"]] == [1, 2]
    snap = client.get("/api/f/%s/snapshot?id=2" % fid)[1]
    assert snap["changes"][0]["path"] == "app.js"
    diff = client.get("/api/f/%s/diff?id=2&path=app.js" % fid)[1]
    assert diff["hunks"]
    res = client.post("/api/f/%s/restore" % fid, {"id": 1, "paths": [str(root / "app.js")]})[1]
    assert res["changed"] == 1 and (root / "app.js").read_text() == "console.log(1)\n"


def test_goback_points_and_back_preview(client, fake_home):
    root = make_project(fake_home)
    fid, _ = add_and_wait(client, root, protect=False)
    pts = client.get("/api/f/%s/goback" % fid)[1]
    assert [p["key"] for p in pts["presets"]] == ["5m", "1h", "morning", "yesterday"]
    assert pts["presets"][0]["snapshot_id"] is None and "started protecting" in pts["presets"][0]["hint"]
    assert len(pts["points"]) == 1
    p = client.post("/api/f/%s/preview" % fid, {"kind": "back", "snapshot_id": 1})[1]
    assert p["nothing"] and p["summary"]["sentence"].startswith("Your files already match")
    assert client.post("/api/f/%s/preview" % fid, {"kind": "back"})[0] == 400


# ------------------------------------------------------------ lifecycle
def test_rewind_app_starts_once_and_stops(tmp_path, fake_home):
    env = {"HOME": str(fake_home), "REWIND_APP_PORT": "0", "REWIND_NO_NOTIFY": "1"}
    first = run_rewind(["app", "--no-open"], cwd=tmp_path, env=env)
    try:
        assert first.returncode == 0, first.stderr
        url = first.stdout.strip().split("running at ")[1]
        second = run_rewind(["app", "--no-open"], cwd=tmp_path, env=env)
        assert second.stdout.strip().split("running at ")[1] == url  # reused, same instance
        status = run_rewind(["app", "status"], cwd=tmp_path, env=env)
        assert status.returncode == 0 and "pid" in status.stdout
        with OPENER.open(urllib.request.Request(url), timeout=5) as r:
            assert r.status == 200
    finally:
        stopped = run_rewind(["app", "stop"], cwd=tmp_path, env=env)
    assert "Stopped" in stopped.stdout
    assert run_rewind(["app", "status"], cwd=tmp_path, env=env).returncode == 1
    assert not (rewind_home() / "app.json").exists()


def test_quit_endpoint_shuts_server_down(fake_home):
    server, url, token, api = app.make_server(0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    c = Client(url.split("/?")[0], token)
    assert c.post("/api/quit")[1] == {"quitting": True}
    t.join(5)
    assert not t.is_alive()
    server.server_close()


def test_redo_after_undo(client, fake_home):
    root = make_project(fake_home)
    fid, _ = add_and_wait(client, root, protect=False)
    repo = Repo.find(root)
    (root / "app.js").write_text("console.log('agent')\n")
    repo.capture()
    assert client.get("/api/f/%s/overview" % fid)[1]["redo"] is False
    p = client.post("/api/f/%s/preview" % fid, {"kind": "redo"})[1]
    assert p["nothing"] and "right after an undo" in p["message"]
    p = client.post("/api/f/%s/preview" % fid, {"kind": "undo"})[1]
    client.post("/api/f/%s/apply" % fid, {"kind": "undo", "expect": p["target_id"]})
    assert (root / "app.js").read_text() == "console.log(1)\n"
    assert client.get("/api/f/%s/overview" % fid)[1]["redo"] is True
    p = client.post("/api/f/%s/preview" % fid, {"kind": "redo"})[1]
    assert p["summary"]["sentence"] == "1 file will change back."
    res = client.post("/api/f/%s/apply" % fid, {"kind": "redo", "expect": p["target_id"]})[1]
    assert res["kind"] == "redo" and (root / "app.js").read_text() == "console.log('agent')\n"
