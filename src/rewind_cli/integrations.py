"""Connecting Rewind to AI tools: agent hooks and MCP server entries.

Every connection is a *plan* first: which file changes, what it looks like
before and after, and where the backup goes. ``apply`` then backs the file up
to ``~/.rewind/backups/`` and writes it. The app shows the plan to the user
before anything is written; ``rewind mcp install`` prints it.

Config locations (from each tool's documentation):

=================  ==========================================  =============================================
tool               this folder                                 all folders (user level)
=================  ==========================================  =============================================
Claude Code hooks  .claude/settings.json (+ .claude/commands)  ~/.claude/settings.json (+ ~/.claude/commands)
Codex hooks        .codex/hooks.json                           ~/.codex/hooks.json
Gemini CLI hooks   .gemini/settings.json                       ~/.gemini/settings.json
Cursor hooks       .cursor/hooks.json                          ~/.cursor/hooks.json
Claude Code MCP    .mcp.json  ("mcpServers")                   `claude mcp add --scope user`
Cursor MCP         .cursor/mcp.json ("mcpServers")             ~/.cursor/mcp.json
VS Code MCP        .vscode/mcp.json ("servers")                <VS Code user dir>/mcp.json
Claude Desktop     n/a                                         claude_desktop_config.json ("mcpServers")
Windsurf           n/a                                         ~/.codeium/windsurf/mcp_config.json
=================  ==========================================  =============================================
"""

import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import hooks
from .store import RewindError, rewind_home

SERVER_NAME = "rewind"


# ------------------------------------------------------------------ helpers
def rewind_invocation():
    """(command, leading args) that start Rewind from a GUI app with a minimal PATH."""
    exe = shutil.which("rewind")
    if exe:
        return str(Path(exe).resolve()) if os.name != "nt" else exe, []
    return sys.executable, ["-m", "rewind_cli"]


def _appdata():
    return Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))


def claude_desktop_config_path(platform=None):
    platform = platform or sys.platform
    if platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if platform.startswith("win"):
        return _appdata() / "Claude" / "claude_desktop_config.json"
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "Claude" / "claude_desktop_config.json"


def vscode_user_dir(platform=None):
    platform = platform or sys.platform
    if platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Code" / "User"
    if platform.startswith("win"):
        return _appdata() / "Code" / "User"
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "Code" / "User"


def _app_exists(*names):
    if sys.platform != "darwin":
        return False
    return any((Path(base) / (n + ".app")).exists() for n in names for base in ("/Applications", Path.home() / "Applications"))


# --------------------------------------------------------------------- tools
TOOLS = [
    {"id": "claude-code", "title": "Claude Code", "kind": "cli",
     "blurb": "Anthropic's coding agent in the terminal and IDEs."},
    {"id": "codex", "title": "Codex CLI", "kind": "cli", "blurb": "OpenAI's coding agent for the terminal."},
    {"id": "gemini", "title": "Gemini CLI", "kind": "cli", "blurb": "Google's coding agent for the terminal."},
    {"id": "cursor", "title": "Cursor", "kind": "app", "blurb": "The AI code editor."},
    {"id": "claude-desktop", "title": "Claude Desktop", "kind": "app", "blurb": "The Claude app for Mac and Windows."},
    {"id": "windsurf", "title": "Windsurf", "kind": "app", "blurb": "The Windsurf AI editor."},
    {"id": "vscode", "title": "VS Code", "kind": "app", "blurb": "Visual Studio Code with Copilot agent mode."},
]
TOOL_IDS = [t["id"] for t in TOOLS]
HOOK_AGENT = {"claude-code": "claude", "codex": "codex", "gemini": "gemini", "cursor": "cursor"}
MCP_TOOLS = ("claude-code", "cursor", "vscode", "claude-desktop", "windsurf")


def detect(tool_id):
    home = Path.home()
    w = shutil.which
    if tool_id == "claude-code":
        return bool(w("claude") or (home / ".claude").exists())
    if tool_id == "codex":
        return bool(w("codex") or (home / ".codex").exists())
    if tool_id == "gemini":
        return bool(w("gemini") or (home / ".gemini").exists())
    if tool_id == "cursor":
        return bool(w("cursor") or w("cursor-agent") or (home / ".cursor").exists() or _app_exists("Cursor"))
    if tool_id == "claude-desktop":
        return bool(claude_desktop_config_path().parent.exists() or _app_exists("Claude"))
    if tool_id == "windsurf":
        return bool(w("windsurf") or (home / ".codeium" / "windsurf").exists() or _app_exists("Windsurf"))
    if tool_id == "vscode":
        return bool(w("code") or vscode_user_dir().exists() or _app_exists("Visual Studio Code"))
    raise RewindError("unknown tool %r" % tool_id)


def scopes(tool_id, kind):
    """Which scopes a (tool, kind) supports: 'folder' and/or 'global'."""
    if kind == "hooks":
        return ["folder", "global"] if tool_id in HOOK_AGENT else []
    if kind == "mcp":
        if tool_id in ("claude-desktop", "windsurf"):
            return ["global"]
        if tool_id in ("claude-code", "cursor", "vscode"):
            return ["folder", "global"]
    return []


def kinds(tool_id):
    out = []
    if tool_id in HOOK_AGENT:
        out.append("hooks")
    if tool_id in MCP_TOOLS:
        out.append("mcp")
    return out


# ------------------------------------------------------------------- files
def config_path(tool_id, kind, scope, folder=None):
    if scope == "folder" and not folder:
        raise RewindError("choose a folder for a per-folder connection")
    base = Path(folder) if scope == "folder" else Path.home()
    if scope not in scopes(tool_id, kind):
        raise RewindError("%s does not support %s %s" % (tool_id, "per-folder" if scope == "folder" else "user-level", kind))
    if kind == "hooks":
        return hooks.settings_path(base, HOOK_AGENT[tool_id])
    if tool_id == "claude-code":
        return base / ".mcp.json" if scope == "folder" else Path.home() / ".claude.json"
    if tool_id == "cursor":
        return base / ".cursor" / "mcp.json"
    if tool_id == "vscode":
        return base / ".vscode" / "mcp.json" if scope == "folder" else vscode_user_dir() / "mcp.json"
    if tool_id == "claude-desktop":
        return claude_desktop_config_path()
    if tool_id == "windsurf":
        return Path.home() / ".codeium" / "windsurf" / "mcp_config.json"
    raise RewindError("unknown tool %r" % tool_id)


_COMMENT = re.compile(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/', re.S)
_TRAILING = re.compile(r'("(?:\\.|[^"\\])*")|,(\s*[}\]])')


def load_json_file(path):
    """Read a JSON (or JSON-with-comments) config file. Returns (data, had_comments)."""
    if not path.exists():
        return {}, False
    text = path.read_text("utf-8")
    if not text.strip():
        return {}, False
    try:
        data = json.loads(text)
        had_comments = False
    except ValueError:
        stripped = _COMMENT.sub(lambda m: m.group(1) or "", text)
        stripped = _TRAILING.sub(lambda m: m.group(1) or m.group(2), stripped)
        try:
            data = json.loads(stripped)
        except ValueError as exc:
            raise RewindError("%s is not valid JSON (%s). Fix it by hand first; Rewind won't overwrite it." % (path, exc))
        had_comments = True
    if not isinstance(data, dict):
        raise RewindError("%s does not contain a JSON object" % path)
    return data, had_comments


def mcp_entry(tool_id, scope):
    command, lead = rewind_invocation()
    args = lead + ["mcp"]
    if scope == "folder" and tool_id in ("cursor", "vscode"):
        args += ["--folder", "${workspaceFolder}"]
    if tool_id == "vscode":
        return {"type": "stdio", "command": command, "args": args}
    return {"command": command, "args": args}


def _servers_key(tool_id):
    return "servers" if tool_id == "vscode" else "mcpServers"


def compute_mcp(data, tool_id, scope, connect=True):
    data = json.loads(json.dumps(data))
    key = _servers_key(tool_id)
    servers = data.get(key)
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        raise RewindError('the "%s" key is not an object' % key)
    if connect:
        servers[SERVER_NAME] = mcp_entry(tool_id, scope)
        data[key] = servers
    else:
        servers.pop(SERVER_NAME, None)
        if servers:
            data[key] = servers
        else:
            data.pop(key, None)
    return data


def _dump(data):
    return json.dumps(data, indent=2) + "\n"


class Plan:
    """One proposed change to one config file (plus, for Claude Code hooks, its slash commands)."""

    def __init__(self, tool_id, kind, scope, folder, action, path, before, after, had_comments=False,
                 extra_files=None, command=None):
        self.tool_id, self.kind, self.scope, self.folder, self.action = tool_id, kind, scope, folder, action
        self.path, self.before, self.after = path, before, after
        self.had_comments = had_comments
        self.extra_files = extra_files or []  # [(path, text or None for delete)]
        self.command = command  # argv to run instead of writing (Claude Code user-level MCP)

    @property
    def changed(self):
        return self.before != self.after or bool(self.extra_files) or bool(self.command)

    def diff(self):
        if self.command:
            return []
        a = self.before.splitlines() if self.before else []
        b = self.after.splitlines() if self.after else []
        return list(difflib.unified_diff(a, b, "before", "after", lineterm="", n=2))[2:]

    def to_json(self):
        notes = []
        if self.had_comments:
            notes.append("This file has comments. Rewind keeps a backup, but comments are not preserved in the rewritten file.")
        if self.command:
            notes.append("Rewind runs this command for you; Claude Code itself updates its settings.")
        return {
            "tool": self.tool_id, "kind": self.kind, "scope": self.scope, "action": self.action,
            "path": str(self.path), "exists": self.path.exists(), "changed": self.changed, "diff": self.diff(),
            "extra_files": [{"path": str(p), "action": "write" if t is not None else "delete"} for p, t in self.extra_files],
            "command": self.command, "notes": notes, "backup_dir": str(backup_dir()),
        }


def plan(tool_id, kind, scope, folder=None, action="connect"):
    if tool_id not in TOOL_IDS:
        raise RewindError("unknown tool %r (choose from %s)" % (tool_id, ", ".join(TOOL_IDS)))
    if kind not in kinds(tool_id):
        raise RewindError("%s has no %s integration" % (tool_id, kind))
    if action not in ("connect", "disconnect"):
        raise RewindError("action must be connect or disconnect")
    path = config_path(tool_id, kind, scope, folder)
    connect = action == "connect"
    if kind == "mcp" and tool_id == "claude-code" and scope == "global":
        command, lead = rewind_invocation()
        argv = (["claude", "mcp", "add", "--scope", "user", SERVER_NAME, "--", command] + lead + ["mcp"]) if connect \
            else ["claude", "mcp", "remove", "--scope", "user", SERVER_NAME]
        return Plan(tool_id, kind, scope, folder, action, path, "", "", command=argv)
    data, had_comments = load_json_file(path)
    before = path.read_text("utf-8") if path.exists() else ""
    if kind == "hooks":
        agent = HOOK_AGENT[tool_id]
        new = hooks.compute_install(data, agent) if connect else hooks.compute_uninstall(data, agent)
    else:
        new = compute_mcp(data, tool_id, scope, connect)
    after = _dump(new)
    if not connect and not path.exists():
        after = ""
    elif not had_comments and path.exists() and json.loads(before or "{}") == new:
        after = before  # semantically unchanged: don't reformat the user's file
    extra = []
    if kind == "hooks" and tool_id == "claude-code":
        base = Path(folder) if scope == "folder" else Path.home()
        cdir = hooks.commands_dir(base)
        for name, text in hooks.slash_commands().items():
            p = cdir / name
            ours = p.exists() and hooks.COMMAND_MARKER in p.read_text("utf-8", errors="replace")
            if connect and (not p.exists() or (ours and p.read_text("utf-8") != text)):
                extra.append((p, text))
            elif not connect and ours:
                extra.append((p, None))
    return Plan(tool_id, kind, scope, folder, action, path, before, after, had_comments, extra)


# ------------------------------------------------------------------ backups
def backup_dir():
    return rewind_home() / "backups"


def backup(path):
    """Copy ``path`` into ~/.rewind/backups/ and return the copy's path (None if it didn't exist)."""
    path = Path(path)
    if not path.exists():
        return None
    d = backup_dir()
    d.mkdir(parents=True, exist_ok=True)
    flat = re.sub(r"[^A-Za-z0-9._-]+", "_", str(path).strip("/\\"))[-120:]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = d / ("%s-%s" % (stamp, flat))
    n = 1
    while dest.exists():
        n += 1
        dest = d / ("%s-%d-%s" % (stamp, n, flat))
    shutil.copy2(str(path), str(dest))
    return dest


def apply(p, runner=subprocess.run):
    """Back up and write a plan. Returns {"path", "backup", "changed", "ran"}."""
    out = {"path": str(p.path), "backup": None, "changed": False, "ran": None, "files": []}
    if p.command:
        if not shutil.which(p.command[0]):
            raise RewindError("the `claude` command isn't on your PATH. Run this yourself instead: %s"
                              % " ".join(p.command))
        bk = backup(p.path)
        out["backup"] = str(bk) if bk else None
        res = runner(p.command, capture_output=True, text=True, timeout=60)
        if res.returncode != 0 and p.action == "connect":
            raise RewindError("`%s` failed: %s" % (" ".join(p.command), (res.stderr or res.stdout).strip()[:300]))
        out["ran"] = p.command
        out["changed"] = True
        return out
    if p.before != p.after:
        bk = backup(p.path)
        out["backup"] = str(bk) if bk else None
        if p.after == "" and p.action == "disconnect":
            pass
        else:
            p.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.path.with_name(".%s.rewind-tmp" % p.path.name)
            tmp.write_text(p.after, "utf-8")
            os.replace(str(tmp), str(p.path))
        out["changed"] = True
    for fp, text in p.extra_files:
        if text is None:
            try:
                fp.unlink()
            except OSError:
                pass
        else:
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(text, "utf-8")
        out["files"].append(str(fp))
        out["changed"] = True
    if p.kind == "hooks" and p.tool_id == "claude-code" and p.action == "disconnect":
        base = Path(p.folder) if p.scope == "folder" else Path.home()
        cdir = hooks.commands_dir(base)
        try:
            if cdir.exists() and not any(cdir.iterdir()):
                cdir.rmdir()
        except OSError:
            pass
    return out


# ------------------------------------------------------------------- status
def is_connected(tool_id, kind, scope, folder=None):
    try:
        path = config_path(tool_id, kind, scope, folder)
    except RewindError:
        return False
    if kind == "hooks":
        if scope == "folder":
            return bool(hooks.installed(Path(folder), HOOK_AGENT[tool_id]))
        return bool(hooks.installed(Path.home(), HOOK_AGENT[tool_id]))
    try:
        data, _ = load_json_file(path)
    except (RewindError, OSError):
        return False
    if tool_id == "claude-code" and scope == "global":
        servers = data.get("mcpServers") or {}
    else:
        servers = data.get(_servers_key(tool_id)) or {}
    return isinstance(servers, dict) and SERVER_NAME in servers


def overview(folder=None):
    """Everything the 'Connect AI tools' screen shows."""
    out = []
    for t in TOOLS:
        item = dict(t)
        item["detected"] = detect(t["id"])
        item["integrations"] = []
        for kind in kinds(t["id"]):
            row = {"kind": kind, "scopes": scopes(t["id"], kind), "status": {}, "paths": {}}
            for scope in row["scopes"]:
                if scope == "folder" and not folder:
                    continue
                try:
                    row["paths"][scope] = str(config_path(t["id"], kind, scope, folder))
                except RewindError:
                    continue
                row["status"][scope] = is_connected(t["id"], kind, scope, folder)
            item["integrations"].append(row)
        out.append(item)
    return out


def manual_snippet(tool_id, scope="global"):
    """The JSON a user would paste by hand (for docs and `rewind mcp config`)."""
    entry = mcp_entry(tool_id, scope)
    return _dump({_servers_key(tool_id): {SERVER_NAME: entry}})
