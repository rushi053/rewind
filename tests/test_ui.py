import json
import threading
import urllib.error
import urllib.request

import pytest

from conftest import read, write
from rewind_cli.ui import make_server

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never proxy localhost


@pytest.fixture
def server(project):
    project.capture()
    write(project, "src/app.py", "print('v2')\n")
    project.capture()
    srv, url, token = make_server(project)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = url.split("/?")[0]
    yield base, token, project
    srv.shutdown()
    srv.server_close()


def get(url, token=None, host=None):
    req = urllib.request.Request(url)
    if token:
        req.add_header("X-Rewind-Token", token)
    if host:
        req.add_header("Host", host)
    with OPENER.open(req, timeout=5) as r:
        return r.status, r.read()


def post(url, token, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    req.add_header("X-Rewind-Token", token)
    req.add_header("Content-Type", "application/json")
    with OPENER.open(req, timeout=10) as r:
        return json.loads(r.read())


def test_index_requires_token(server):
    base, token, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        get(base + "/")
    assert e.value.code == 403
    status, body = get(base + "/?t=" + token)
    assert status == 200 and b"<title>Rewind</title>" in body
    assert b"https://" not in body.split(b"<script>")[0], "no external resources"


def test_api_requires_header_token(server):
    base, token, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        get(base + "/api/timeline?t=" + token)  # query token is not enough for the API
    assert e.value.code == 403


def test_rejects_foreign_host_header(server):
    base, token, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        get(base + "/api/timeline", token, host="evil.example.com")
    assert e.value.code == 403


def test_timeline_snapshot_diff(server):
    base, token, project = server
    _, body = get(base + "/api/timeline", token)
    data = json.loads(body)
    assert [s["id"] for s in data["snapshots"]] == [1, 2]
    _, body = get(base + "/api/snapshot?id=2", token)
    snap = json.loads(body)
    assert snap["changes"] == [{"status": "M", "path": "src/app.py", "old_size": 15, "new_size": 12, "old_link": None, "new_link": None, "plus": 1, "minus": 1}]
    _, body = get(base + "/api/diff?id=2&path=src/app.py", token)
    d = json.loads(body)
    kinds = [row[0] for row in d["hunks"][0]]
    assert "chg" in kinds or ("del" in kinds and "add" in kinds)


def test_restore_file_and_undo_via_api(server):
    base, token, project = server
    res = post(base + "/api/restore", token, {"id": 1, "paths": ["src/app.py"]})
    assert res["changed"] == 1 and res["safety_id"] == 2
    assert read(project, "src/app.py") == "print('hello')\n"
    res = post(base + "/api/undo", token, {})
    assert read(project, "src/app.py") == "print('v2')\n"


def test_api_errors_are_json(server):
    base, token, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        get(base + "/api/snapshot?id=999", token)
    assert e.value.code == 400
    assert "no snapshot" in json.loads(e.value.read())["error"]


def test_bundled_fonts_are_served_offline(server):
    base, _, _ = server
    status, body = get(base + "/fonts/Geist-Variable.woff2")
    assert status == 200 and body[:4] == b"wOF2"
    status, body = get(base + "/fonts/GeistMono-Variable.woff2")
    assert status == 200 and body[:4] == b"wOF2"
    with pytest.raises(urllib.error.HTTPError) as e:
        get(base + "/fonts/nope.woff2")
    assert e.value.code == 404
