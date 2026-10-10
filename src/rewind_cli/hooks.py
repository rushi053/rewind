"""Agent hook installation and the hook entry point (`rewind hook exec`).

Formats follow each tool's published hook documentation:

* Claude Code  -- .claude/settings.json      PreToolUse/PostToolUse, exit 2 + stderr blocks
* Codex CLI    -- .codex/hooks.json          PreToolUse/PostToolUse (Bash, apply_patch), exit 2 blocks
* Gemini CLI   -- .gemini/settings.json      BeforeTool/AfterTool, exit 2 + stderr blocks
* Cursor       -- .cursor/hooks.json         beforeShellExecution (JSON permission), afterFileEdit
"""

import json
import os
import re
import shutil
import sys
import time
import traceback
from pathlib import Path

from . import guard
from .store import NotInitialized, Repo, RewindError

AGENTS = ("claude", "codex", "gemini", "cursor")
AGENT_TITLES = {"claude": "Claude Code", "codex": "Codex", "gemini": "Gemini CLI", "cursor": "Cursor"}
MARKER = re.compile(r"rewind(?:_cli)?\b.*\bhook exec ")


def hook_command(agent, event):
    exe = shutil.which("rewind")
    if exe:
        base = "rewind"
    else:
        base = '"%s" -m rewind_cli' % sys.executable
    return "%s hook exec %s %s" % (base, agent, event)


def _is_ours(cmd):
    return isinstance(cmd, str) and bool(MARKER.search(cmd))


def settings_path(root, agent, local=False):
    root = Path(root)
    if agent == "claude":
        return root / ".claude" / ("settings.local.json" if local else "settings.json")
    if agent == "codex":
        return root / ".codex" / "hooks.json"
    if agent == "gemini":
        return root / ".gemini" / "settings.json"
    if agent == "cursor":
        return root / ".cursor" / "hooks.json"
    raise RewindError("unknown agent %r (choose from: %s)" % (agent, ", ".join(AGENTS)))


def _load(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text("utf-8") or "{}")
    except ValueError as exc:
        raise RewindError("%s is not valid JSON (%s); fix it before installing hooks" % (path, exc))
    if not isinstance(data, dict):
        raise RewindError("%s does not contain a JSON object" % path)
    return data


def _save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", "utf-8")


def _desired(agent):
    """Return {event: [hook groups]} for Claude-style formats, or Cursor's flat lists."""
    if agent == "claude":
        matcher = "Write|Edit|MultiEdit|NotebookEdit|Bash"
        return {
            ev: [{"matcher": matcher, "hooks": [{"type": "command", "command": hook_command("claude", ev), "timeout": 30}]}]
            for ev in ("PreToolUse", "PostToolUse")
        }
    if agent == "codex":
        matcher = "Bash|apply_patch"
        return {
            ev: [{"matcher": matcher, "hooks": [{"type": "command", "command": hook_command("codex", ev), "timeout": 30}]}]
            for ev in ("PreToolUse", "PostToolUse")
        }
    if agent == "gemini":
        matcher = "run_shell_command|write_file|replace"
        return {
            ev: [{"matcher": matcher, "hooks": [{"name": "rewind", "type": "command", "command": hook_command("gemini", ev), "timeout": 30000}]}]
            for ev in ("BeforeTool", "AfterTool")
        }
    if agent == "cursor":
        return {
            "beforeShellExecution": [{"command": hook_command("cursor", "beforeShellExecution")}],
            "afterFileEdit": [{"command": hook_command("cursor", "afterFileEdit")}],
            "afterShellExecution": [{"command": hook_command("cursor", "afterShellExecution")}],
        }
    raise RewindError("unknown agent %r" % agent)


def _strip_ours(hooks_obj, agent):
    """Remove previously installed rewind hooks, keeping everything else."""
    cleaned = {}
    for event, groups in hooks_obj.items():
        if not isinstance(groups, list):
            cleaned[event] = groups
            continue
        new_groups = []
        for g in groups:
            if agent == "cursor":
                if isinstance(g, dict) and _is_ours(g.get("command")):
                    continue
                new_groups.append(g)
                continue
            if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                inner = [h for h in g["hooks"] if not (isinstance(h, dict) and _is_ours(h.get("command")))]
                if not inner:
                    continue
                g = dict(g, hooks=inner)
            new_groups.append(g)
        if new_groups:
            cleaned[event] = new_groups
    return cleaned


def install(root, agent, local=False):
    path = settings_path(root, agent, local)
    data = _load(path)
    if agent == "cursor":
        data.setdefault("version", 1)
    hooks_obj = data.get("hooks") or {}
    if not isinstance(hooks_obj, dict):
        raise RewindError("%s has a non-object \"hooks\" key" % path)
    hooks_obj = _strip_ours(hooks_obj, agent)
    for event, groups in _desired(agent).items():
        hooks_obj.setdefault(event, [])
        hooks_obj[event].extend(groups)
    data["hooks"] = hooks_obj
    _save(path, data)
    return path


def uninstall(root, agent, local=False):
    path = settings_path(root, agent, local)
    if not path.exists():
        return None
    data = _load(path)
    hooks_obj = _strip_ours(data.get("hooks") or {}, agent)
    if hooks_obj:
        data["hooks"] = hooks_obj
    else:
        data.pop("hooks", None)
    _save(path, data)
    return path


def installed(root, agent):
    paths = [settings_path(root, agent)]
    if agent == "claude":
        paths.append(settings_path(root, agent, local=True))
    for path in paths:
        if path.exists():
            try:
                text = path.read_text("utf-8")
            except OSError:
                continue
            if MARKER.search(text):
                return path
    return None


def detect_agents(root):
    """Agents that look present: a config dir in the project or the CLI on PATH."""
    found = []
    probes = {
        "claude": (".claude", "claude"),
        "codex": (".codex", "codex"),
        "gemini": (".gemini", "gemini"),
        "cursor": (".cursor", "cursor"),
    }
    for agent, (dirname, exe) in probes.items():
        if (Path(root) / dirname).exists() or shutil.which(exe) or (Path.home() / dirname).exists():
            found.append(agent)
    return found


# --------------------------------------------------------------------- exec
class HookEvent:
    def __init__(self, agent, event, data):
        self.agent = agent
        self.raw_event = event
        self.data = data
        self.phase = "pre" if event in ("PreToolUse", "BeforeTool", "beforeShellExecution") else "post"
        tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
        tool = data.get("tool_name") or ""
        command = None
        file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path")
        if agent == "cursor":
            if event in ("beforeShellExecution", "afterShellExecution"):
                tool, command = "Shell", data.get("command")
            else:
                tool, file_path = "Edit", data.get("file_path")
        elif agent == "gemini":
            if tool == "run_shell_command":
                command = tool_input.get("command")
        elif tool == "Bash":
            command = tool_input.get("command")
        self.tool = tool or "tool"
        self.command = command if isinstance(command, str) else None
        self.file_path = file_path if isinstance(file_path, str) else None
        roots = data.get("workspace_roots") or []
        self.cwd = data.get("cwd") or (roots[0] if roots else None) or os.getcwd()
        self.session_key = data.get("session_id") or data.get("conversation_id")

    def detail(self):
        if self.command:
            return self.command
        return self.file_path or ""


def _respond(agent, phase, allow, reason=None):
    """Return (exit_code, stdout, stderr) in each agent's dialect."""
    if agent == "cursor":
        if phase == "pre":
            if allow:
                return 0, json.dumps({"permission": "allow"}), ""
            return 0, json.dumps({"permission": "deny", "user_message": reason, "agent_message": reason}), ""
        return 0, "", ""
    if agent == "gemini":
        if allow:
            return 0, "{}", ""
        return 2, "", reason + "\n"
    if allow:
        return 0, "", ""
    return 2, "", reason + "\n"


def _session_for(repo, ev):
    env_sid = os.environ.get("REWIND_SESSION")
    if env_sid:
        try:
            return repo.get_session(env_sid)["id"], False
        except RewindError:
            pass
    if not ev.session_key:
        return None, False
    external = "%s:%s" % (ev.agent, ev.session_key)
    sess = repo.session_for_external(external)
    if sess:
        return sess["id"], False
    sess = repo.new_session(name=AGENT_TITLES.get(ev.agent, ev.agent), agent=ev.agent, external=external)
    return sess["id"], True


def run_hook(agent, event, stdin_text, env_cwd=None):
    try:
        data = json.loads(stdin_text) if stdin_text.strip() else {}
        if not isinstance(data, dict):
            data = {}
    except ValueError:
        data = {}
    ev = HookEvent(agent, event, data)
    try:
        repo = Repo.find(ev.cwd)
    except NotInitialized:
        return _respond(agent, ev.phase, True)
    try:
        return _run(repo, ev)
    except Exception:  # never break the agent because of us
        try:
            with open(str(repo.store / "hook-errors.log"), "a") as fh:
                fh.write("%s %s %s\n%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), agent, event, traceback.format_exc()))
        except OSError:
            pass
        return _respond(agent, ev.phase, True)


def _run(repo, ev):
    session, is_new = _session_for(repo, ev)
    trigger, message = "hook", "%s %s" % ("before" if ev.phase == "pre" else "after", ev.tool)
    if ev.phase == "pre":
        findings, decision = [], "allow"
        if ev.command:
            findings, decision = guard.evaluate(ev.command, repo.config, cwd=ev.cwd, root=str(repo.root))
        elif ev.file_path and ev.tool in ("Write", "write_file"):
            target = Path(ev.file_path)
            if not target.is_absolute():
                target = Path(ev.cwd) / target
            if guard._is_env_name(str(target)) and target.exists() and "overwrite-env" not in (repo.config.get("guard.disable") or []):
                mode = repo.config.get("guard.mode") or "block"
                if mode != "off":
                    findings, decision = [guard.Finding("overwrite-env", target.name)], mode
        if decision == "block":
            reasons = "; ".join(f.message() for f in findings)
            text = (
                "Rewind seatbelt blocked this command: %s. "
                "Do not try to work around this block. If the user really wants it, ask them to run it "
                "themselves or allow it with: rewind guard allow '<pattern>'." % reasons
            )
            # record the block on the timeline (a metadata-only snapshot: no new content is stored)
            _capture(repo, session, "guard", "blocked: " + "; ".join(f.reason for f in findings), ev, start=is_new, force=True)
            return _respond(ev.agent, "pre", False, text)
        if findings:
            trigger = "guard"
            message = "seatbelt: " + "; ".join(f.reason for f in findings)
            if decision == "warn":
                sys.stderr.write("rewind: warning: %s\n" % message)
        _capture(repo, session, trigger, message, ev, start=is_new)
        return _respond(ev.agent, "pre", True)
    _capture(repo, session, trigger, message, ev, start=is_new)
    return _respond(ev.agent, "post", True)


def _relative_detail(repo, ev):
    detail = ev.detail()
    if ev.file_path and not ev.command:
        try:
            return str(Path(detail).resolve().relative_to(repo.root.resolve()).as_posix())
        except (ValueError, OSError):
            return detail
    return detail


def _capture(repo, session, trigger, message, ev, start=False, force=False):
    prev_head = repo.head()[0]
    meta, _files, head = repo.capture(message=message, trigger=trigger, session=session, tool=ev.tool,
                                      detail=_relative_detail(repo, ev), force=force)
    if session:
        sess = repo.get_session(session)
        if sess.get("start_snapshot") is None:
            if ev.phase == "post" and prev_head is not None:
                # first sign of this session came after an edit: best known prior state
                sess["start_snapshot"] = prev_head
            else:
                sess["start_snapshot"] = head if meta is None else meta["id"]
        sess["last"] = time.time()
        sess["end_snapshot"] = meta["id"] if meta else head
        repo.save_session(sess)
    return meta
