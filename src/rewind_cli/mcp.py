"""`rewind mcp`: a Model Context Protocol server over stdio (JSON-RPC 2.0, stdlib only).

Implements the parts of MCP a tools-only server needs: ``initialize`` with
protocol version negotiation, ``notifications/initialized``, ``ping``,
``tools/list`` and ``tools/call``. Messages are newline-delimited JSON on
stdin/stdout; anything diagnostic goes to stderr.

Tools that change files (``undo_last``, ``restore``, ``recover_deleted``)
only *preview* unless called with ``confirm: true``.
"""

import io
import json
import os
import sys
import time
import traceback
from pathlib import Path

from . import __version__, plain, registry
from .store import NotInitialized, Repo, RewindError

LATEST = "2025-06-18"
SUPPORTED = ("2025-06-18", "2025-03-26", "2024-11-05")

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603

INSTRUCTIONS = (
    "Rewind keeps saved versions of every file in the user's project (including untracked files and .git). "
    "Call checkpoint before risky multi-file work. Use list_history and show_changes to inspect what changed. "
    "undo_last, restore and recover_deleted only preview unless you pass confirm=true: show the preview to the "
    "user and get their agreement before confirming. Use check_command to ask whether a shell command is destructive."
)

_FOLDER = {"type": "string", "description": "Absolute path of the project folder. Optional when the server was started in (or for) a single project."}
_CONFIRM = {"type": "boolean", "default": False, "description": "Apply the change. Without it the tool only previews what would change."}

TOOLS = [
    {
        "name": "checkpoint",
        "title": "Save a checkpoint",
        "description": "Save a version of every file in the project now, labelled with a message. No-op if nothing changed since the last saved version.",
        "inputSchema": {"type": "object", "properties": {"message": {"type": "string", "description": "Short label, e.g. 'before refactoring auth'"}, "folder": _FOLDER}},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "list_history",
        "title": "List saved versions",
        "description": "List the most recent saved versions of the project, newest first, with who changed what.",
        "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20}, "folder": _FOLDER}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "show_changes",
        "title": "Show what a version changed",
        "description": "Show the files a saved version changed compared with the version before it. Set diff=true for a unified diff (optionally limited to one path).",
        "inputSchema": {"type": "object", "required": ["id"], "properties": {
            "id": {"type": ["integer", "string"], "description": "Version id from list_history (e.g. 12), or HEAD / HEAD~1"},
            "diff": {"type": "boolean", "default": False}, "path": {"type": "string"}, "folder": _FOLDER}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "undo_last",
        "title": "Undo the last change",
        "description": "Put the project back to the previous saved version that differs from the current files. Previews by default; pass confirm=true to apply. A safety copy of the current files is saved first.",
        "inputSchema": {"type": "object", "properties": {"confirm": _CONFIRM, "dry_run": {"type": "boolean"}, "folder": _FOLDER}},
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "restore",
        "title": "Restore a saved version",
        "description": "Restore the whole project, or only some paths, to a saved version. Previews by default; pass confirm=true to apply. A safety copy of the current files is saved first.",
        "inputSchema": {"type": "object", "required": ["id"], "properties": {
            "id": {"type": ["integer", "string"]},
            "paths": {"type": "array", "items": {"type": "string"}, "description": "Project-relative paths or globs; omit for the whole project"},
            "confirm": _CONFIRM, "dry_run": {"type": "boolean"}, "folder": _FOLDER}},
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "recover_deleted",
        "title": "Recover deleted files",
        "description": "Bring back deleted files matching a path or glob (e.g. '.env', 'src/**/*.py') from the newest version that had them. Touches nothing else. Previews by default; pass confirm=true to apply.",
        "inputSchema": {"type": "object", "required": ["pattern"], "properties": {
            "pattern": {"type": "string"}, "confirm": _CONFIRM, "dry_run": {"type": "boolean"}, "folder": _FOLDER}},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "check_command",
        "title": "Check a shell command",
        "description": "Ask Rewind's seatbelt whether a shell command is destructive (rm -rf, git reset --hard, DROP TABLE, ...). Returns allow, warn or block with reasons. Runs nothing.",
        "inputSchema": {"type": "object", "required": ["command"], "properties": {"command": {"type": "string"}, "folder": _FOLDER}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]
TOOL_NAMES = {t["name"] for t in TOOLS}


class RpcError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


class ToolError(Exception):
    """A tool ran but could not do what was asked: reported as isError=true, not a protocol error."""


class Server:
    def __init__(self, folder=None, cwd=None):
        self.folder = folder
        self.cwd = cwd or os.getcwd()
        self.protocol = None
        self.client = None
        self.initialized = False

    # ------------------------------------------------------------ dispatch
    def handle(self, msg):
        """Handle one decoded JSON-RPC message (or batch). Returns a response object, list, or None."""
        if isinstance(msg, list):
            if not msg:
                return _error(None, INVALID_REQUEST, "empty batch")
            out = [r for r in (self.handle(m) for m in msg) if r is not None]
            return out or None
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error(msg.get("id") if isinstance(msg, dict) else None, INVALID_REQUEST, "not a JSON-RPC 2.0 message")
        method = msg.get("method")
        mid = msg.get("id")
        is_notification = "id" not in msg
        if method is None:
            return None  # a response to something we never send; ignore
        params = msg.get("params") or {}
        try:
            if not isinstance(params, dict):
                raise RpcError(INVALID_PARAMS, "params must be an object")
            result = self._dispatch(method, params)
        except RpcError as exc:
            if is_notification:
                return None
            return _error(mid, exc.code, exc.message, exc.data)
        except Exception as exc:  # never crash the server
            sys.stderr.write("rewind mcp: internal error: %s\n%s" % (exc, traceback.format_exc()))
            if is_notification:
                return None
            return _error(mid, INTERNAL_ERROR, "internal error: %s" % exc)
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _dispatch(self, method, params):
        if method == "initialize":
            return self._initialize(params)
        if method in ("notifications/initialized", "initialized"):
            self.initialized = True
            return None
        if method.startswith("notifications/"):
            return None
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [self._tool_view(t) for t in TOOLS]}
        if method == "tools/call":
            return self._call(params)
        if method in ("resources/list", "prompts/list"):
            raise RpcError(METHOD_NOT_FOUND, "%s is not supported (Rewind only provides tools)" % method)
        raise RpcError(METHOD_NOT_FOUND, "method not found: %s" % method)

    def _initialize(self, params):
        requested = params.get("protocolVersion")
        self.protocol = requested if requested in SUPPORTED else LATEST
        self.client = params.get("clientInfo") or {}
        info = {"name": "rewind", "version": __version__}
        if self.protocol >= "2025-06-18":
            info["title"] = "Rewind"
        return {
            "protocolVersion": self.protocol,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": info,
            "instructions": INSTRUCTIONS,
        }

    def _tool_view(self, tool):
        t = dict(tool)
        if (self.protocol or LATEST) < "2025-06-18":
            t.pop("title", None)
        if (self.protocol or LATEST) < "2025-03-26":
            t.pop("annotations", None)
        return t

    def _call(self, params):
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in TOOL_NAMES:
            raise RpcError(INVALID_PARAMS, "unknown tool: %s" % name)
        if not isinstance(args, dict):
            raise RpcError(INVALID_PARAMS, "arguments must be an object")
        schema = next(t for t in TOOLS if t["name"] == name)["inputSchema"]
        missing = [k for k in schema.get("required", []) if args.get(k) in (None, "")]
        if missing:
            raise RpcError(INVALID_PARAMS, "missing required argument(s): %s" % ", ".join(missing))
        real_stdout = sys.stdout
        sys.stdout = sys.stderr  # nothing a tool prints may corrupt the protocol stream
        try:
            text, data = getattr(self, "tool_" + name)(args)
            is_error = False
        except (ToolError, RewindError) as exc:
            text, data, is_error = str(exc), {"error": str(exc)}, True
        finally:
            sys.stdout = real_stdout
        result = {"content": [{"type": "text", "text": text}], "isError": is_error}
        if (self.protocol or LATEST) >= "2025-06-18" and data is not None:
            result["structuredContent"] = data
        return result

    # ------------------------------------------------------------- helpers
    def repo(self, args):
        folder = args.get("folder") or self.folder
        if folder:
            folder = os.path.expanduser(str(folder))
            if "${" in folder:
                raise ToolError("the folder path %r was not expanded by the client; pass an absolute path" % folder)
            try:
                return Repo.find(folder)
            except NotInitialized:
                raise ToolError("%s is not protected by Rewind yet. Open the Rewind app or run `rewind init` there." % folder)
        try:
            return Repo.find(self.cwd)
        except NotInitialized:
            pass
        folders = [f for f in registry.load() if Path(f["path"]).is_dir()]
        if len(folders) == 1:
            return Repo.find(folders[0]["path"])
        if not folders:
            raise ToolError("No project is protected by Rewind yet. Open the Rewind app (`rewind app`) or run `rewind init` in the project.")
        raise ToolError("Several projects are protected; pass folder= one of: %s" % ", ".join(f["path"] for f in folders))

    @staticmethod
    def _confirmed(args):
        if args.get("dry_run") is True:
            return False
        return args.get("confirm") is True

    @staticmethod
    def _abs(repo, p):
        return p if os.path.isabs(p) or any(ch in p for ch in "*?[") else str(repo.root / p)

    def _preview_text(self, title, summary, sid=None):
        lines = [title, summary["sentence"]]
        for label, key in (("Comes back", "come_back"), ("Changes back", "change_back"), ("Removed", "removed")):
            items = summary[key]
            if items:
                lines.append("%s (%d): %s%s" % (label, len(items), ", ".join(items[:15]), " …" if len(items) > 15 else ""))
        if summary["git_internal"]:
            lines.append("Plus %d of Git's internal files." % summary["git_internal"])
        return "\n".join(lines)

    def _restore_result(self, res, what):
        summary = plain.describe_plan(res.plan)
        n = summary["total"] - len(res.errors)
        text = "%s: %d file%s changed. The previous state was saved as version #%s; restore it to undo this." % (
            what, n, "" if n == 1 else "s", res.safety_id)
        if res.errors:
            text += "\nErrors: " + "; ".join("%s: %s" % e for e in res.errors[:10])
        return text, {"applied": True, "summary": summary, "safety_id": res.safety_id, "post_id": res.post_id,
                      "errors": [{"path": p, "error": e} for p, e in res.errors]}

    # --------------------------------------------------------------- tools
    def tool_checkpoint(self, args):
        repo = self.repo(args)
        message = str(args.get("message") or "").strip()
        meta, files, head = repo.capture(message=message or "checkpoint (MCP)", trigger="manual", tool="mcp")
        if meta is None:
            return ("Nothing changed since version #%s, so no new checkpoint was needed." % head,
                    {"created": False, "id": head, "files": len(files)})
        return ("Saved checkpoint #%d (%d files)%s." % (meta["id"], len(files), (": " + message) if message else ""),
                {"created": True, "id": meta["id"], "files": len(files), "stats": meta["stats"]})

    def tool_list_history(self, args):
        repo = self.repo(args)
        try:
            limit = max(1, min(200, int(args.get("limit") or 20)))
        except (TypeError, ValueError):
            raise ToolError("limit must be a number")
        sessions = {s["id"]: s for s in repo.list_sessions()}
        snaps = [m for m in repo.list_snapshots() if m.get("trigger") != "safety"][-limit:]
        items = [plain.describe_snapshot(m, sessions) for m in reversed(snaps)]
        if not items:
            return "No saved versions yet.", {"versions": []}
        lines = ["%d most recent saved versions of %s (newest first):" % (len(items), repo.root)]
        for it, m in zip(items, reversed(snaps)):
            files = (" (%s)" % ", ".join(it["files"])) if it["files"] else ""
            lines.append("#%d  %s  %s — %s%s" % (it["id"], plain.when(it["time"]), it["title"], it["summary"], files))
        return "\n".join(lines), {"folder": str(repo.root), "versions": [
            dict(it, stats=m.get("stats"), trigger=m.get("trigger")) for it, m in zip(items, reversed(snaps))]}

    def tool_show_changes(self, args):
        from .diff import diff_manifests, unified

        repo = self.repo(args)
        sid = repo.resolve(args["id"])
        meta = repo.get_snapshot(sid)
        parent = meta.get("parent")
        old = repo.snapshot_files(parent) if parent is not None and repo.snap_path(parent).exists() else {}
        changes = diff_manifests(old, repo.snapshot_files(sid))
        path = args.get("path")
        if path:
            path = path.strip("/")
            changes = [c for c in changes if c["path"] == path or c.get("old_path") == path or c["path"].startswith(path + "/")]
        desc = plain.describe_snapshot(meta, {s["id"]: s for s in repo.list_sessions()})
        lines = ["Version #%d (%s): %s" % (sid, plain.when(meta["time"]), desc["title"])]
        word = {"A": "added", "M": "edited", "D": "deleted", "R": "renamed"}
        for c in changes[:200]:
            lines.append("  %s %s%s" % (word[c["status"]], c["path"], (" (from %s)" % c["old_path"]) if c.get("old_path") else ""))
        if len(changes) > 200:
            lines.append("  … and %d more" % (len(changes) - 200))
        if not changes:
            lines.append("  no file changes")
        if args.get("diff"):
            budget = 20000
            for c in changes:
                if plain.is_git_internal(c["path"]) and not path:
                    continue
                chunk = "\n".join(unified(repo, c))
                if len(chunk) > budget:
                    lines.append("\n[diff truncated; ask for a single path]")
                    break
                budget -= len(chunk)
                lines.append("\n" + chunk)
        return "\n".join(lines), {"id": sid, "time": meta["time"], "title": desc["title"], "changes": [
            {"status": c["status"], "path": c["path"], "old_path": c.get("old_path")} for c in changes]}

    def tool_undo_last(self, args):
        from . import restore

        repo = self.repo(args)
        try:
            preview = restore.undo(repo, dry_run=True)
        except RewindError as exc:
            return str(exc)[0].upper() + str(exc)[1:] + ".", {"applied": False, "nothing": True}
        target = preview.plan.target_id
        meta = repo.get_snapshot(target)
        summary = plain.describe_plan(preview.plan)
        if not self._confirmed(args):
            text = self._preview_text("Preview: undo back to version #%d (%s)." % (target, plain.when(meta["time"])), summary)
            return text + "\nNothing has changed yet. Call undo_last again with confirm=true to apply.", {
                "applied": False, "preview": True, "target_id": target, "summary": summary}
        res = restore.undo(repo)
        registry.log("restored", "An AI assistant undid the last change (via MCP)", folder=str(repo.root))
        return self._restore_result(res, "Undone, back to version #%d" % target)

    def tool_restore(self, args):
        from . import restore

        repo = self.repo(args)
        sid = repo.resolve(args["id"])
        paths = args.get("paths") or None
        if paths is not None and (not isinstance(paths, list) or not all(isinstance(p, str) for p in paths)):
            raise ToolError("paths must be a list of strings")
        abs_paths = [self._abs(repo, p) for p in paths] if paths else None
        preview = restore.restore(repo, sid, abs_paths, dry_run=True)
        summary = plain.describe_plan(preview.plan)
        meta = repo.get_snapshot(sid)
        if not preview.plan:
            return "Nothing to restore: the files already match version #%d." % sid, {"applied": False, "summary": summary}
        if not self._confirmed(args):
            text = self._preview_text("Preview: restore %s to version #%d (%s)." % (
                ", ".join(paths) if paths else "the whole project", sid, plain.when(meta["time"])), summary)
            return text + "\nNothing has changed yet. Call restore again with confirm=true to apply.", {
                "applied": False, "preview": True, "target_id": sid, "summary": summary}
        res = restore.restore(repo, sid, abs_paths)
        registry.log("restored", "An AI assistant restored version #%d (via MCP)" % sid, folder=str(repo.root))
        return self._restore_result(res, "Restored to version #%d" % sid)

    def tool_recover_deleted(self, args):
        from . import restore

        repo = self.repo(args)
        pattern = str(args["pattern"])
        pats = [self._abs(repo, pattern)]
        preview = restore.recover(repo, pats, dry_run=True)
        files = [w[0] for w in preview.plan.writes]
        if not files:
            return "No deleted file matching %r was found in any saved version." % pattern, {"applied": False, "files": []}
        if not self._confirmed(args):
            text = "Preview: %d deleted file%s can be brought back: %s" % (
                len(files), "" if len(files) == 1 else "s", ", ".join(files[:25]) + (" …" if len(files) > 25 else ""))
            return text + "\nNothing has changed yet. Call recover_deleted again with confirm=true to apply.", {
                "applied": False, "preview": True, "files": files, "sources": getattr(preview, "sources", {})}
        res = restore.recover(repo, pats)
        registry.log("recovered", "An AI assistant brought back %d deleted file%s (via MCP)" % (len(files), "" if len(files) == 1 else "s"),
                     folder=str(repo.root))
        text, data = self._restore_result(res, "Brought back %d file%s" % (len(files), "" if len(files) == 1 else "s"))
        data["files"] = files
        return text, data

    def tool_check_command(self, args):
        from . import guard
        from .config import Config

        command = str(args["command"])
        try:
            repo = self.repo(args)
            cfg, root = repo.config, str(repo.root)
        except ToolError:
            cfg, root = Config(Path(".rewind-defaults-only", "none.json")), self.cwd
        findings, decision = guard.evaluate(command, cfg, cwd=root, root=root)
        if not findings:
            return "Looks safe: the seatbelt found nothing destructive in that command.", {"decision": "allow", "findings": []}
        verdict = {"block": "BLOCK", "snapshot": "ALLOW AFTER SAVING", "warn": "WARN"}[decision]
        lines = ["%s: %s" % (verdict, command)] + ["- %s (%s, rule %s)" % (f.message(), f.severity, f.rule) for f in findings]
        if decision == "block":
            lines.append("Rewind's hooks would block this. Don't try to work around it; ask the user.")
        return "\n".join(lines), {"decision": decision, "findings": [
            {"rule": f.rule, "severity": f.severity, "reason": f.reason, "detail": f.detail} for f in findings]}


def _error(mid, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": mid, "error": err}


def serve(stdin=None, stdout=None, folder=None):
    """Run the stdio loop until EOF."""
    stdin = stdin or io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    stdout = stdout or sys.stdout
    if folder and "${" in folder:
        sys.stderr.write("rewind mcp: ignoring unexpanded --folder %s\n" % folder)
        folder = None
    server = Server(folder=folder)
    sys.stderr.write("rewind mcp %s ready (protocol %s)\n" % (__version__, LATEST))
    sys.stderr.flush()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError as exc:
            resp = _error(None, PARSE_ERROR, "parse error: %s" % exc)
        else:
            resp = server.handle(msg)
        if resp is not None:
            stdout.write(json.dumps(resp, separators=(",", ":")) + "\n")
            stdout.flush()
    return 0
