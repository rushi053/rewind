import json
import os
import subprocess
import sys

import pytest

from conftest import read, write
from rewind_cli import registry
from rewind_cli.mcp import LATEST, Server

EXPECTED_TOOLS = {"checkpoint", "list_history", "show_changes", "undo_last", "restore", "recover_deleted", "check_command"}


def rpc(server, method, params=None, mid=1):
    msg = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        msg["params"] = params
    return server.handle(msg)


def call(server, name, **args):
    resp = rpc(server, "tools/call", {"name": name, "arguments": args})
    assert "result" in resp, resp
    return resp["result"]


@pytest.fixture
def server(project):
    srv = Server(folder=str(project.root))
    rpc(srv, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
    srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return srv


# ---------------------------------------------------------------- protocol
def test_initialize_negotiates_protocol_version(project):
    for requested, expected in (("2025-06-18", "2025-06-18"), ("2025-03-26", "2025-03-26"),
                                ("2024-11-05", "2024-11-05"), ("1999-01-01", LATEST), (None, LATEST)):
        srv = Server(folder=str(project.root))
        params = {"capabilities": {}, "clientInfo": {"name": "t"}}
        if requested:
            params["protocolVersion"] = requested
        res = rpc(srv, "initialize", params)["result"]
        assert res["protocolVersion"] == expected
        assert res["capabilities"] == {"tools": {"listChanged": False}}
        assert res["serverInfo"]["name"] == "rewind" and res["instructions"]


def test_tools_list_shape_depends_on_version(project):
    new = Server(folder=str(project.root))
    rpc(new, "initialize", {"protocolVersion": "2025-06-18"})
    tools = rpc(new, "tools/list")["result"]["tools"]
    assert {t["name"] for t in tools} == EXPECTED_TOOLS
    for t in tools:
        assert t["inputSchema"]["type"] == "object" and t["description"] and "title" in t and "annotations" in t
    destructive = {t["name"] for t in tools if t["annotations"].get("destructiveHint")}
    assert destructive == {"undo_last", "restore"}
    old = Server(folder=str(project.root))
    rpc(old, "initialize", {"protocolVersion": "2024-11-05"})
    for t in rpc(old, "tools/list")["result"]["tools"]:
        assert "title" not in t and "annotations" not in t


def test_protocol_errors(server):
    assert rpc(server, "ping") == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert rpc(server, "resources/list")["error"]["code"] == -32601
    assert rpc(server, "no/such")["error"]["code"] == -32601
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}}) is None
    assert server.handle({"id": 3, "method": "ping"})["error"]["code"] == -32600
    assert rpc(server, "tools/call", {"name": "format_disk", "arguments": {}})["error"]["code"] == -32602
    assert rpc(server, "tools/call", {"name": "restore", "arguments": {}})["error"]["code"] == -32602
    assert rpc(server, "tools/call", {"name": "checkpoint", "arguments": "nope"})["error"]["code"] == -32602
    assert server.handle([]) ["error"]["code"] == -32600
    batch = server.handle([{"jsonrpc": "2.0", "id": 7, "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/x"}])
    assert batch == [{"jsonrpc": "2.0", "id": 7, "result": {}}]
    assert server.handle({"jsonrpc": "2.0", "id": 9, "result": {}}) is None  # a response: ignored


# ------------------------------------------------------------------- tools
def test_checkpoint_and_history(server, project):
    project.capture(message="initial", trigger="init")
    res = call(server, "checkpoint", message="nothing new")
    assert not res["isError"] and "Nothing changed" in res["content"][0]["text"]
    write(project, "src/app.py", "print('v2')\n")
    res = call(server, "checkpoint", message="before refactor")
    assert res["structuredContent"]["created"] and "before refactor" in res["content"][0]["text"]
    sid = res["structuredContent"]["id"]
    assert project.get_snapshot(sid)["message"] == "before refactor"
    hist = call(server, "list_history", limit=5)
    versions = hist["structuredContent"]["versions"]
    assert versions[0]["id"] == sid and versions[0]["title"] == "Checkpoint: before refactor"
    assert "#%d" % sid in hist["content"][0]["text"]


def test_show_changes_with_diff(server, project):
    project.capture()
    write(project, "src/app.py", "print('changed')\n")
    meta, _f, _h = project.capture()
    res = call(server, "show_changes", id=meta["id"], diff=True)
    text = res["content"][0]["text"]
    assert "edited src/app.py" in text and "+print('changed')" in text
    assert res["structuredContent"]["changes"] == [{"status": "M", "path": "src/app.py", "old_path": None}]
    assert call(server, "show_changes", id="HEAD", path="README.md")["structuredContent"]["changes"] == []
    bad = call(server, "show_changes", id=999)
    assert bad["isError"] and "no snapshot" in bad["content"][0]["text"]


def test_undo_last_previews_unless_confirmed(server, project):
    project.capture()
    write(project, "src/app.py", "broken\n")
    project.capture()
    preview = call(server, "undo_last")
    assert not preview["isError"] and preview["structuredContent"]["preview"] is True
    assert "1 file will change back" in preview["content"][0]["text"] and "confirm=true" in preview["content"][0]["text"]
    assert read(project, "src/app.py") == "broken\n"
    still = call(server, "undo_last", confirm=True, dry_run=True)
    assert still["structuredContent"]["applied"] is False and read(project, "src/app.py") == "broken\n"
    done = call(server, "undo_last", confirm=True)
    assert done["structuredContent"]["applied"] is True
    assert read(project, "src/app.py") == "print('hello')\n"
    assert registry.activity()[0]["kind"] == "restored"


def test_restore_paths_and_recover_deleted(server, project):
    first, _f, _h = project.capture()
    write(project, "src/app.py", "v2\n")
    write(project, "README.md", "readme v2\n")
    (project.root / ".env").unlink()
    project.capture()
    res = call(server, "restore", id=first["id"], paths=["README.md"])
    assert res["structuredContent"]["preview"] and read(project, "README.md") == "readme v2\n"
    res = call(server, "restore", id=first["id"], paths=["README.md"], confirm=True)
    assert read(project, "README.md") == "# demo\n" and read(project, "src/app.py") == "v2\n"
    bad = call(server, "restore", id=first["id"], paths="README.md")
    assert bad["isError"]
    rec = call(server, "recover_deleted", pattern=".env")
    assert rec["structuredContent"]["files"] == [".env"] and not (project.root / ".env").exists()
    rec = call(server, "recover_deleted", pattern=".env", confirm=True)
    assert read(project, ".env") == "SECRET=abc\n"
    none = call(server, "recover_deleted", pattern="nope/*.txt")
    assert "No deleted file" in none["content"][0]["text"]


def test_check_command(server):
    res = call(server, "check_command", command="rm -rf ~")
    assert res["structuredContent"]["decision"] == "block" and "BLOCK" in res["content"][0]["text"]
    assert call(server, "check_command", command="ls -la")["structuredContent"] == {"decision": "allow", "findings": []}


def test_structured_content_only_for_new_protocol(project):
    project.capture()
    srv = Server(folder=str(project.root))
    rpc(srv, "initialize", {"protocolVersion": "2025-03-26"})
    res = call(srv, "list_history")
    assert "structuredContent" not in res and res["content"][0]["type"] == "text"


def test_folder_resolution_via_registry(tmp_path, project, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    srv = Server(cwd=str(elsewhere))
    rpc(srv, "initialize", {"protocolVersion": LATEST})
    res = call(srv, "list_history")
    assert res["isError"] and "No project is protected" in res["content"][0]["text"]
    registry.register(project.root)
    project.capture()
    assert not call(srv, "list_history")["isError"]  # the only registered folder
    other = tmp_path / "other"
    other.mkdir()
    registry.setup_folder(other)
    registry.register(other)
    res = call(srv, "list_history")
    assert res["isError"] and "pass folder=" in res["content"][0]["text"]
    assert not call(srv, "list_history", folder=str(project.root))["isError"]
    res = call(srv, "list_history", folder="${workspaceFolder}")
    assert res["isError"] and "not expanded" in res["content"][0]["text"]
    res = call(srv, "check_command", command="git reset --hard")  # works without a project too
    assert res["structuredContent"]["decision"] == "block"


# ------------------------------------------------------------- over stdio
def test_stdio_transport_end_to_end(project):
    project.capture()
    write(project, "src/app.py", "edited\n")
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "checkpoint", "arguments": {"message": "over stdio"}}},
    ]
    stdin = "\n".join(json.dumps(m) for m in msgs) + "\nthis is not json\n\n"
    env = dict(os.environ)
    proc = subprocess.run([sys.executable, "-m", "rewind_cli", "mcp", "--folder", str(project.root)], input=stdin,
                          capture_output=True, text=True, timeout=60, env=env, cwd=str(project.root.parent))
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    responses = [json.loads(l) for l in lines]  # every stdout line is a JSON-RPC message
    assert [r.get("id") for r in responses] == [1, 2, 3, None]
    assert responses[0]["result"]["protocolVersion"] == "2025-06-18"
    assert {t["name"] for t in responses[1]["result"]["tools"]} == EXPECTED_TOOLS
    assert responses[2]["result"]["structuredContent"]["created"] is True
    assert responses[3]["error"]["code"] == -32700
    assert "ready" in proc.stderr
    assert project.get_snapshot(project.snapshot_ids()[-1])["message"] == "over stdio"
