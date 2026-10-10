import json

import pytest

from conftest import read, write
from rewind_cli import hooks


def payload(**kw):
    base = {"session_id": "sess-1", "cwd": None, "hook_event_name": "PreToolUse"}
    base.update(kw)
    return json.dumps(base)


def test_claude_install_writes_documented_format(project):
    path = hooks.install(project.root, "claude")
    assert path == project.root / ".claude" / "settings.json"
    data = json.loads(path.read_text())
    for event in ("PreToolUse", "PostToolUse"):
        groups = data["hooks"][event]
        assert len(groups) == 1
        g = groups[0]
        assert set(g["matcher"].split("|")) >= {"Write", "Edit", "MultiEdit", "Bash"}
        assert g["hooks"][0]["type"] == "command"
        assert g["hooks"][0]["command"].endswith("hook exec claude " + event)


def test_install_is_idempotent_and_preserves_other_settings(project):
    settings = project.root / ".claude" / "settings.json"
    settings.parent.mkdir()
    existing = {
        "permissions": {"allow": ["Bash(npm test)"]},
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "./my-linter.sh"}]}]},
    }
    settings.write_text(json.dumps(existing))
    hooks.install(project.root, "claude")
    hooks.install(project.root, "claude")
    data = json.loads(settings.read_text())
    assert data["permissions"] == existing["permissions"]
    pre = data["hooks"]["PreToolUse"]
    commands = [h["command"] for g in pre for h in g["hooks"]]
    assert "./my-linter.sh" in commands
    assert sum("rewind" in c and "hook exec" in c for c in commands) == 1
    hooks.uninstall(project.root, "claude")
    data = json.loads(settings.read_text())
    assert [h["command"] for g in data["hooks"]["PreToolUse"] for h in g["hooks"]] == ["./my-linter.sh"]
    assert "PostToolUse" not in data["hooks"]


def test_claude_local_settings(project):
    path = hooks.install(project.root, "claude", local=True)
    assert path.name == "settings.local.json"
    assert hooks.installed(project.root, "claude") == path


@pytest.mark.parametrize("agent,relpath,events", [
    ("codex", ".codex/hooks.json", ["PreToolUse", "PostToolUse"]),
    ("gemini", ".gemini/settings.json", ["BeforeTool", "AfterTool"]),
])
def test_claude_style_agents(project, agent, relpath, events):
    path = hooks.install(project.root, agent)
    assert path == project.root / relpath
    data = json.loads(path.read_text())
    assert sorted(data["hooks"]) == sorted(events)
    for ev in events:
        assert data["hooks"][ev][0]["hooks"][0]["type"] == "command"


def test_cursor_format(project):
    path = hooks.install(project.root, "cursor")
    data = json.loads(path.read_text())
    assert data["version"] == 1
    assert "beforeShellExecution" in data["hooks"]
    assert "afterFileEdit" in data["hooks"]
    assert data["hooks"]["beforeShellExecution"][0]["command"].endswith("hook exec cursor beforeShellExecution")


def test_invalid_settings_json_is_reported(project):
    p = project.root / ".claude" / "settings.json"
    p.parent.mkdir()
    p.write_text("{ not json")
    from rewind_cli.store import RewindError

    with pytest.raises(RewindError):
        hooks.install(project.root, "claude")


def test_claude_pre_hook_snapshots_and_creates_session(project):
    project.capture()
    write(project, "src/app.py", "user edit before the agent")
    code, out, err = hooks.run_hook("claude", "PreToolUse", payload(
        cwd=str(project.root), tool_name="Edit", tool_input={"file_path": str(project.root / "src/app.py")}))
    assert code == 0 and out == ""
    meta = project.get_snapshot(project.snapshot_ids()[-1])
    assert meta["trigger"] == "hook" and meta["tool"] == "Edit" and meta["detail"] == "src/app.py"
    sess = project.get_session(meta["session"])
    assert sess["external"] == "claude:sess-1"
    assert sess["start_snapshot"] == meta["id"]
    # the agent edits; PostToolUse records it in the same session
    write(project, "src/app.py", "agent edit")
    hooks.run_hook("claude", "PostToolUse", payload(cwd=str(project.root), hook_event_name="PostToolUse", tool_name="Edit",
                                                     tool_input={"file_path": "src/app.py"}))
    last = project.get_snapshot(project.snapshot_ids()[-1])
    assert last["session"] == sess["id"]
    assert len(project.list_sessions()) == 1


def test_claude_bash_guard_blocks_with_exit_2(project):
    code, out, err = hooks.run_hook("claude", "PreToolUse", payload(
        cwd=str(project.root), tool_name="Bash", tool_input={"command": "git reset --hard HEAD~5"}))
    assert code == 2
    assert "git reset --hard" in err


def test_guard_snapshot_mode_allows_and_labels(project):
    project.config.set("guard.mode", "snapshot")
    write(project, "x.txt", "pending")
    code, out, err = hooks.run_hook("claude", "PreToolUse", payload(
        cwd=str(project.root), tool_name="Bash", tool_input={"command": "git clean -fdx"}))
    assert code == 0
    meta = project.get_snapshot(project.snapshot_ids()[-1])
    assert meta["trigger"] == "guard"
    assert "git clean" in meta["message"]


def test_write_tool_overwriting_env_is_flagged(project):
    code, _, err = hooks.run_hook("claude", "PreToolUse", payload(
        cwd=str(project.root), tool_name="Write", tool_input={"file_path": str(project.root / ".env"), "content": "X=1"}))
    assert code == 2 and ".env" in err


def test_allowlisted_command_passes(project):
    project.config.append("guard.allow", "git reset --hard*")
    code, _, _ = hooks.run_hook("claude", "PreToolUse", payload(
        cwd=str(project.root), tool_name="Bash", tool_input={"command": "git reset --hard"}))
    assert code == 0


def test_codex_uses_same_protocol(project):
    code, _, err = hooks.run_hook("codex", "PreToolUse", payload(
        cwd=str(project.root), tool_name="Bash", tool_input={"command": "rm -rf ~"}))
    assert code == 2 and "home" in err


def test_gemini_dialect(project):
    code, out, err = hooks.run_hook("gemini", "BeforeTool", payload(
        cwd=str(project.root), hook_event_name="BeforeTool", tool_name="run_shell_command", tool_input={"command": "ls"}))
    assert (code, out) == (0, "{}")
    code, out, err = hooks.run_hook("gemini", "BeforeTool", payload(
        cwd=str(project.root), hook_event_name="BeforeTool", tool_name="run_shell_command", tool_input={"command": "git clean -fdx"}))
    assert code == 2 and "git clean" in err


def test_cursor_dialect(project):
    raw = json.dumps({"conversation_id": "c1", "command": "npm test", "workspace_roots": [str(project.root)]})
    code, out, _ = hooks.run_hook("cursor", "beforeShellExecution", raw)
    assert code == 0 and json.loads(out) == {"permission": "allow"}
    raw = json.dumps({"conversation_id": "c1", "command": "rm -rf ~", "workspace_roots": [str(project.root)]})
    code, out, _ = hooks.run_hook("cursor", "beforeShellExecution", raw)
    resp = json.loads(out)
    assert resp["permission"] == "deny" and "home" in resp["agent_message"]
    write(project, "src/app.py", "cursor edit")
    raw = json.dumps({"conversation_id": "c1", "file_path": str(project.root / "src/app.py"), "workspace_roots": [str(project.root)]})
    assert hooks.run_hook("cursor", "afterFileEdit", raw)[0] == 0


def test_hooks_outside_a_project_are_noops(tmp_path):
    code, out, err = hooks.run_hook("claude", "PreToolUse", payload(cwd=str(tmp_path), tool_name="Bash",
                                                                     tool_input={"command": "git reset --hard"}))
    assert (code, out, err) == (0, "", "")


def test_garbage_stdin_fails_open(project):
    assert hooks.run_hook("claude", "PreToolUse", "not json at all")[0] == 0
    assert hooks.run_hook("cursor", "beforeShellExecution", "[]")[0] == 0


def test_internal_errors_fail_open_and_are_logged(project, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(type(project), "capture", boom)
    code, out, err = hooks.run_hook("claude", "PreToolUse", payload(cwd=str(project.root), tool_name="Edit",
                                                                     tool_input={"file_path": "a"}))
    assert code == 0
    assert "disk on fire" in (project.store / "hook-errors.log").read_text()


def test_rewind_session_env_joins_run_session(project, monkeypatch):
    sess = project.new_session("wrapped")
    monkeypatch.setenv("REWIND_SESSION", sess["id"])
    write(project, "new.py", "x")
    hooks.run_hook("claude", "PreToolUse", payload(cwd=str(project.root), tool_name="Edit", tool_input={"file_path": "new.py"}))
    assert project.get_snapshot(project.snapshot_ids()[-1])["session"] == sess["id"]
    assert len(project.list_sessions()) == 1
