import json
from pathlib import Path

import pytest

from rewind_cli import hooks, integrations
from rewind_cli.store import RewindError, rewind_home


def connect(tool, kind, scope, folder=None):
    p = integrations.plan(tool, kind, scope, folder, "connect")
    return p, integrations.apply(p)


def disconnect(tool, kind, scope, folder=None):
    p = integrations.plan(tool, kind, scope, folder, "disconnect")
    return p, integrations.apply(p)


def load(path):
    return json.loads(Path(path).read_text())


# ---------------------------------------------------------------- MCP configs
def test_claude_desktop_merges_into_existing_config_with_backup(fake_home):
    cfg = fake_home / ".config" / "Claude" / "claude_desktop_config.json"
    cfg.parent.mkdir(parents=True)
    original = {"mcpServers": {"github": {"command": "gh-mcp", "args": ["--x"]}}, "globalShortcut": "Ctrl+Space"}
    cfg.write_text(json.dumps(original))
    p, res = connect("claude-desktop", "mcp", "global")
    assert p.path == cfg and res["changed"]
    data = load(cfg)
    assert data["globalShortcut"] == "Ctrl+Space"
    assert data["mcpServers"]["github"] == original["mcpServers"]["github"]
    entry = data["mcpServers"]["rewind"]
    assert entry["args"][-1] == "mcp" and Path(entry["command"]).is_absolute()
    backup = Path(res["backup"])
    assert backup.parent == rewind_home() / "backups" and json.loads(backup.read_text()) == original
    assert integrations.is_connected("claude-desktop", "mcp", "global")
    # connecting again changes nothing
    again = integrations.plan("claude-desktop", "mcp", "global")
    assert not again.changed
    # disconnect removes only Rewind
    disconnect("claude-desktop", "mcp", "global")
    assert load(cfg) == original
    assert not integrations.is_connected("claude-desktop", "mcp", "global")


def test_claude_desktop_paths_per_platform(fake_home, monkeypatch):
    mac = integrations.claude_desktop_config_path("darwin")
    assert mac == fake_home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    monkeypatch.setenv("APPDATA", str(fake_home / "AppData" / "Roaming"))
    assert integrations.claude_desktop_config_path("win32") == fake_home / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"


def test_creates_missing_file_and_removes_key_when_empty(fake_home):
    cfg = fake_home / ".codeium" / "windsurf" / "mcp_config.json"
    p, res = connect("windsurf", "mcp", "global")
    assert p.path == cfg and res["backup"] is None
    assert list(load(cfg)["mcpServers"]) == ["rewind"]
    disconnect("windsurf", "mcp", "global")
    assert load(cfg) == {}


def test_vscode_uses_servers_key_type_and_workspace_folder(tmp_path, fake_home):
    folder = tmp_path / "proj"
    (folder / ".vscode").mkdir(parents=True)
    (folder / ".vscode" / "mcp.json").write_text(
        '{\n  // my servers\n  "servers": {\n    "other": {"type": "stdio", "command": "x"},\n  },\n  "inputs": []\n}\n')
    p = integrations.plan("vscode", "mcp", "folder", str(folder))
    assert p.had_comments and any("comments" in n for n in p.to_json()["notes"])
    integrations.apply(p)
    data = load(folder / ".vscode" / "mcp.json")
    assert data["inputs"] == [] and "other" in data["servers"]
    assert data["servers"]["rewind"]["type"] == "stdio"
    assert data["servers"]["rewind"]["args"][-3:] == ["mcp", "--folder", "${workspaceFolder}"]
    user = integrations.plan("vscode", "mcp", "global")
    assert user.path == fake_home / ".config" / "Code" / "User" / "mcp.json"
    assert "--folder" not in integrations.mcp_entry("vscode", "global")["args"]


def test_cursor_project_and_global(tmp_path, fake_home):
    folder = tmp_path / "proj"
    folder.mkdir()
    connect("cursor", "mcp", "folder", str(folder))
    assert load(folder / ".cursor" / "mcp.json")["mcpServers"]["rewind"]["args"][-1] == "${workspaceFolder}"
    connect("cursor", "mcp", "global")
    assert "--folder" not in load(fake_home / ".cursor" / "mcp.json")["mcpServers"]["rewind"]["args"]


def test_claude_code_project_file_and_user_scope_command(tmp_path, fake_home, monkeypatch):
    folder = tmp_path / "proj"
    folder.mkdir()
    connect("claude-code", "mcp", "folder", str(folder))
    assert load(folder / ".mcp.json")["mcpServers"]["rewind"]["args"][-1] == "mcp"
    p = integrations.plan("claude-code", "mcp", "global")
    assert p.command[:6] == ["claude", "mcp", "add", "--scope", "user", "rewind"] and p.command[-1] == "mcp"
    assert p.to_json()["command"] == p.command
    monkeypatch.setattr(integrations.shutil, "which", lambda name: None)
    with pytest.raises(RewindError, match="isn't on your PATH"):
        integrations.apply(p)
    ran = []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(integrations.shutil, "which", lambda name: "/usr/bin/" + name)
    res = integrations.apply(p, runner=lambda argv, **kw: ran.append(argv) or Done())
    assert ran == [p.command] and res["ran"] == p.command
    # status reads ~/.claude.json, which `claude mcp add --scope user` maintains
    (fake_home / ".claude.json").write_text(json.dumps({"mcpServers": {"rewind": {"command": "rewind"}}, "numStartups": 3}))
    assert integrations.is_connected("claude-code", "mcp", "global")


def test_invalid_json_is_never_overwritten(fake_home):
    cfg = fake_home / ".cursor" / "mcp.json"
    cfg.parent.mkdir()
    cfg.write_text('{"mcpServers": {broken')
    with pytest.raises(RewindError, match="not valid JSON"):
        integrations.plan("cursor", "mcp", "global")
    assert cfg.read_text() == '{"mcpServers": {broken'


def test_unsupported_scopes_and_unknown_tools(tmp_path):
    with pytest.raises(RewindError):
        integrations.plan("claude-desktop", "mcp", "folder", str(tmp_path))
    with pytest.raises(RewindError):
        integrations.plan("windsurf", "hooks", "global")
    with pytest.raises(RewindError):
        integrations.plan("emacs", "mcp", "global")
    with pytest.raises(RewindError):
        integrations.plan("cursor", "mcp", "folder", None)


def test_manual_snippet_is_valid_json():
    snip = json.loads(integrations.manual_snippet("windsurf"))
    assert snip["mcpServers"]["rewind"]["args"][-1] == "mcp"
    assert "servers" in json.loads(integrations.manual_snippet("vscode", "folder"))


# ------------------------------------------------------------------- hooks
def test_global_claude_hooks_keep_user_settings_and_add_slash_commands(fake_home):
    settings = fake_home / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}}))
    p = integrations.plan("claude-code", "hooks", "global")
    assert {Path(x["path"]).name for x in p.to_json()["extra_files"]} == {"rewind-undo.md", "rewind-log.md", "rewind-checkpoint.md"}
    res = integrations.apply(p)
    data = load(settings)
    assert data["model"] == "opus" and data["hooks"]["Stop"][0]["hooks"][0]["command"] == "say done"
    assert "hook exec claude PreToolUse" in json.dumps(data["hooks"]["PreToolUse"])
    assert res["backup"] and integrations.is_connected("claude-code", "hooks", "global")
    cmd_dir = fake_home / ".claude" / "commands"
    undo = (cmd_dir / "rewind-undo.md").read_text()
    assert undo.startswith("---\ndescription:") and "undo --dry-run" in undo and hooks.COMMAND_MARKER in undo
    assert '$ARGUMENTS' in (cmd_dir / "rewind-checkpoint.md").read_text()
    disconnect("claude-code", "hooks", "global")
    data = load(settings)
    assert "PreToolUse" not in data["hooks"] and data["hooks"]["Stop"]
    assert not cmd_dir.exists()


@pytest.mark.parametrize("tool,path", [("codex", ".codex/hooks.json"), ("gemini", ".gemini/settings.json"), ("cursor", ".cursor/hooks.json")])
def test_other_agents_hooks_global_and_folder(tool, path, tmp_path, fake_home):
    connect(tool, "hooks", "global")
    assert (fake_home / path).exists() and integrations.is_connected(tool, "hooks", "global")
    folder = tmp_path / "proj"
    folder.mkdir()
    connect(tool, "hooks", "folder", str(folder))
    assert integrations.is_connected(tool, "hooks", "folder", str(folder))
    disconnect(tool, "hooks", "folder", str(folder))
    assert not integrations.is_connected(tool, "hooks", "folder", str(folder))


def test_slash_commands_never_clobber_a_users_own_file(project):
    d = project.root / ".claude" / "commands"
    d.mkdir(parents=True)
    (d / "rewind-log.md").write_text("my own command\n")
    hooks.install(project.root, "claude")
    assert (d / "rewind-log.md").read_text() == "my own command\n"
    assert hooks.COMMAND_MARKER in (d / "rewind-undo.md").read_text()
    hooks.uninstall(project.root, "claude")
    assert (d / "rewind-log.md").exists() and not (d / "rewind-undo.md").exists()


def test_detect_and_overview(fake_home, tmp_path, monkeypatch):
    monkeypatch.setattr(integrations.shutil, "which", lambda name: None)
    assert not integrations.detect("cursor")
    (fake_home / ".cursor").mkdir()
    (fake_home / ".config" / "Claude").mkdir(parents=True)
    assert integrations.detect("cursor") and integrations.detect("claude-desktop") and not integrations.detect("codex")
    folder = tmp_path / "proj"
    folder.mkdir()
    ov = {t["id"]: t for t in integrations.overview(str(folder))}
    assert set(ov) == set(integrations.TOOL_IDS)
    assert [r["kind"] for r in ov["cursor"]["integrations"]] == ["hooks", "mcp"]
    assert ov["claude-desktop"]["integrations"][0]["scopes"] == ["global"]
    assert ov["vscode"]["integrations"][0]["paths"]["folder"].endswith(".vscode/mcp.json")
    no_folder = {t["id"]: t for t in integrations.overview()}
    assert "folder" not in no_folder["cursor"]["integrations"][0]["paths"]


# --------------------------------------------------------------------- CLI
def test_cli_mcp_install_dry_run_and_config(cli, fake_home, project):
    code, out, _ = cli("mcp", "install", "cursor", "--global", "--dry-run")
    assert code == 0 and "dry run" in out and not (fake_home / ".cursor" / "mcp.json").exists()
    code, out, _ = cli("mcp", "install", "cursor", "--global")
    assert code == 0 and "added to cursor" in out
    assert "rewind" in load(fake_home / ".cursor" / "mcp.json")["mcpServers"]
    code, out, _ = cli("mcp", "install", "cursor", "--global")
    assert "nothing to do" in out
    code, out, _ = cli("mcp", "install", "vscode")  # project scope: the current rewind project
    assert (project.root / ".vscode" / "mcp.json").exists()
    code, out, _ = cli("mcp", "config", "claude-desktop")
    assert json.loads(out)["mcpServers"]["rewind"]["args"][-1] == "mcp"
    code, out, _ = cli("mcp", "uninstall", "cursor", "--global")
    assert code == 0 and load(fake_home / ".cursor" / "mcp.json") == {}
    code, _out, err = cli("mcp", "install", "notepad")
    assert code == 1 and "unknown client" in err


def test_cli_hook_install_global(cli, fake_home):
    code, out, _ = cli("hook", "install", "claude", "--global")
    assert code == 0 and str(fake_home / ".claude" / "settings.json") in out and "/rewind-undo" in out
    assert (fake_home / ".claude" / "commands" / "rewind-undo.md").exists()
