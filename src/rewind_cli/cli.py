"""Command-line interface. Heavy modules are imported lazily to keep startup fast."""

import argparse
import os
import sys
import time

from . import __version__, term
from .store import NotInitialized, Repo, RewindError

MAIN_HELP = """\
{title} -- an undo button for AI coding agents.

Snapshots every file in your project (including untracked files like .env and
the .git directory itself) before and while an agent works, so any change can
be rolled back in one command.

{usage}
  rewind <command> [options]

{start}
  init                 Set up Rewind here and offer to install agent hooks
  run -- <cmd>         Wrap an agent session: checkpoint, watch, checkpoint
  hook install <agent> Snapshot before every edit/command (claude, codex, gemini, cursor)
  watch [--daemon]     Auto-snapshot whenever files change

{undo}
  undo [--session]     Revert the last change (or the whole last agent session)
  redo                 Re-apply what the last undo reverted
  back <time>          Return the project to how it was, e.g. `back 10m`
  restore <id> [path]  Restore a snapshot, or just some paths from it
  recover <path|glob>  Bring back deleted files from the latest snapshot that had them

{inspect}
  log                  Timeline of snapshots grouped by session
  show <id>            What a snapshot changed
  diff [id] [id] [path] Colored diff between snapshots or against your files
  status               Store size, latest snapshot, pending changes
  ui                   Local web UI with timeline, diffs and one-click restore

{other}
  snap [-m msg]        Take a checkpoint now
  guard check "<cmd>"  Ask the seatbelt whether a command is destructive
  gc                   Apply the retention policy and free space
  config               Show or change settings
  doctor               Check the store and integrations

{examples}
  rewind init
  rewind run -- claude
  rewind undo --session        # the agent wrecked things: put it all back
  rewind back 15m -y
  rewind recover .env

Run `rewind <command> --help` for details. Docs: https://github.com/rewind-cli/rewind
"""


def _main_help():
    h = term.bold
    return MAIN_HELP.format(
        title=term.bold_cyan("rewind"), usage=h("Usage:"), start=h("Protect a session:"),
        undo=h("Undo:"), inspect=h("Inspect:"), other=h("More:"), examples=h("Examples:"),
    )


def _check():
    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    return "✓" if "utf" in enc else "ok"


def _repo():
    return Repo.find()


def _confirm(prompt, yes):
    if yes:
        return True
    if not sys.stdin or not sys.stdin.isatty():
        raise RewindError("refusing to change files without confirmation in a non-interactive shell; pass -y")
    try:
        answer = input("%s [y/N] " % prompt)
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def _short(path, width):
    if len(path) <= width:
        return path
    return "…" + path[-(width - 1):]


def _print_plan(plan, limit=40):
    rows = []
    for path, _entry, kind in plan.writes:
        rows.append((path, term.yellow("~ restore ") if kind == "modify" else term.green("+ recreate")))
    for path in plan.deletes:
        rows.append((path, term.red("- remove  ")))
    rows.sort()
    for path, label in rows[:limit]:
        print("  %s  %s" % (label, path))
    if len(rows) > limit:
        print(term.dim("  … and %d more" % (len(rows) - limit)))
    c = plan.counts()
    bits = []
    if c["modify"]:
        bits.append(term.yellow("%d restored" % c["modify"]))
    if c["create"]:
        bits.append(term.green("%d recreated" % c["create"]))
    if c["delete"]:
        bits.append(term.red("%d removed" % c["delete"]))
    print("  " + ", ".join(bits))


def _report_result(res, what, redo=False):
    if res.errors:
        for path, msg in res.errors:
            term.err("%s: %s" % (path, msg))
    n = len(res.plan.writes) + len(res.plan.deletes) - len(res.errors)
    back = ("`rewind redo` or `rewind restore %s`" if redo else "`rewind restore %s`") % res.safety_id
    print("%s %s (%d file%s)." % (term.green(_check()), what, n, "" if n == 1 else "s"))
    print(term.dim("  Previous state saved as #%s. Get it back with %s." % (res.safety_id, back)))
    return 1 if res.errors else 0


def _run_restore_flow(preview_fn, apply_fn, what, args, empty_msg="Nothing to restore: files already match.", done=None, redo=False):
    preview = preview_fn()
    plan = preview.plan
    if not plan:
        print(empty_msg)
        return 0
    print(term.bold(what))
    _print_plan(plan)
    if args.dry_run:
        print(term.dim("(dry run: nothing was changed)"))
        return 0
    if not _confirm("Apply?", args.yes):
        print("Aborted.")
        return 1
    return _report_result(apply_fn(), done or what.split(" (")[0].rstrip(":"), redo=redo)


# ---------------------------------------------------------------- commands
def cmd_init(args):
    from .hooks import AGENTS, detect_agents, install
    from .ignore import DEFAULT_IGNORE

    root = os.path.abspath(args.path or os.getcwd())
    try:
        existing = Repo.find(root)
        if os.path.samefile(str(existing.root), root):
            print("Already initialized: %s (store: %s)" % (existing.root, existing.store))
            return 0
    except NotInitialized:
        pass
    repo = Repo.init(root, global_store=args.global_store)
    ignore = repo.root / ".rewindignore"
    if not ignore.exists():
        ignore.write_text(DEFAULT_IGNORE, "utf-8")
    t0 = time.time()
    meta, files, _ = repo.capture(message="initial snapshot", trigger="init")
    took = time.time() - t0
    total = sum(e.get("s", 0) for e in files.values())
    print("%s Initialized Rewind in %s" % (term.green(_check()), repo.root))
    print("  store     %s" % repo.store)
    print("  snapshot  #%d  %d files, %s  (%.2fs)" % (meta["id"] if meta else 0, len(files), term.size(total), took))
    if meta and meta.get("skipped"):
        print(term.yellow("  skipped   %d file(s) over %s MB -- see `rewind doctor`" % (meta["skipped"], repo.config.get("max_file_size_mb"))))
    agents = []
    if args.hooks:
        agents = [a.strip() for a in args.hooks.split(",") if a.strip()]
    elif not args.no_hooks:
        detected = detect_agents(repo.root)
        for agent in detected:
            if args.yes:
                agents.append(agent)
            elif sys.stdin.isatty():
                try:
                    ans = input("Install Rewind hooks for %s? [Y/n] " % agent)
                except EOFError:
                    ans = "n"
                if ans.strip().lower() in ("", "y", "yes"):
                    agents.append(agent)
        if detected and not agents and not sys.stdin.isatty():
            print("  Detected %s. Install hooks with `rewind hook install <agent>`." % ", ".join(detected))
    for agent in agents:
        if agent not in AGENTS:
            raise RewindError("unknown agent %r (choose from %s)" % (agent, ", ".join(AGENTS)))
        path = install(repo.root, agent)
        print("  hooks     %s -> %s" % (agent, os.path.relpath(str(path), str(repo.root))))
    print()
    print("Next: run your agent through Rewind:  %s" % term.bold("rewind run -- claude"))
    print("      or keep a watcher running:     %s" % term.bold("rewind watch --daemon"))
    return 0


def cmd_snap(args):
    repo = _repo()
    t0 = time.time()
    meta, files, head = repo.capture(message=args.message or "", trigger="manual", force=args.force)
    ms = (time.time() - t0) * 1000
    if meta is None:
        if not args.quiet:
            print("Nothing changed since #%s (%d files checked in %.0f ms)." % (head, len(files), ms))
        return 0
    if args.quiet:
        print(meta["id"])
        return 0
    print("%s Snapshot %s  %s  %s" % (
        term.green(_check()), term.bold("#%d" % meta["id"]), term.stats_str(meta["stats"]),
        term.dim("%d files, %.0f ms" % (len(files), ms))))
    return 0


def cmd_watch(args):
    from .watch import Watcher, daemon_pid, start_daemon, stop_daemon

    repo = _repo()
    action = args.action or ("daemon" if args.daemon else "foreground")
    if action == "stop":
        pid = stop_daemon(repo)
        print("Stopped watcher (pid %d)." % pid if pid else "No watcher running.")
        return 0
    if action == "status":
        pid = daemon_pid(repo)
        print("Watcher running (pid %d), log: %s" % (pid, repo.store / "watch.log") if pid else "No watcher running.")
        return 0
    if action in ("start", "daemon") and not args.foreground:
        pid = start_daemon(repo)
        print("%s Watcher started in the background (pid %d). Stop it with `rewind watch stop`." % (term.green(_check()), pid))
        return 0
    import signal

    def on_snap(meta):
        if not args.quiet:
            print("%s  #%d  %s" % (time.strftime("%H:%M:%S"), meta["id"], term.stats_str(meta["stats"])))
            sys.stdout.flush()

    w = Watcher(repo, interval=args.interval, debounce=args.debounce, on_snapshot=on_snap)
    try:
        signal.signal(signal.SIGTERM, lambda *a: w.stop())
    except (ValueError, AttributeError):
        pass
    if not args.quiet:
        print("Watching %s (poll %.1fs, debounce %.1fs). Ctrl-C to stop." % (repo.root, w.interval, w.debounce))
    sys.stdout.flush()
    t = w.start_thread()
    try:
        while t.is_alive():
            t.join(0.5)
    except KeyboardInterrupt:
        w.stop()
        t.join()
    return 0


AGENT_NAMES = {"claude": "claude", "codex": "codex", "gemini": "gemini", "cursor-agent": "cursor",
               "cursor": "cursor", "aider": "aider", "opencode": "opencode", "goose": "goose", "amp": "amp"}


def cmd_run(args):
    import shlex
    import subprocess

    from .diff import diff_manifests, summarize
    from .watch import Watcher

    cmd = list(args.command or [])
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        raise RewindError("nothing to run. Usage: rewind run -- <command> [args...]")
    repo = _repo()
    exe = os.path.basename(cmd[0])
    name = args.name or exe
    command_str = " ".join(shlex.quote(c) for c in cmd)
    sess = repo.new_session(name, agent=AGENT_NAMES.get(exe), command=command_str)
    sid = sess["id"]
    meta, _f, head = repo.capture(message="before " + name, trigger="run-start", session=sid)
    sess = repo.get_session(sid)
    sess["start_snapshot"] = meta["id"] if meta else head
    repo.save_session(sess)
    sys.stderr.write("%s session %s started, checkpoint #%s. Undo it all with `rewind undo --session`.\n"
                     % (term.cyan("rewind:"), term.bold(sid), sess["start_snapshot"]))
    watcher = Watcher(repo, session=sid)
    thread = watcher.start_thread() if not args.no_watch else None
    env = dict(os.environ, REWIND_SESSION=sid)
    started = time.time()
    try:
        proc = subprocess.Popen(cmd, env=env)
    except FileNotFoundError:
        watcher.stop()
        raise RewindError("command not found: %s" % cmd[0])
    except PermissionError:
        watcher.stop()
        raise RewindError("permission denied: %s" % cmd[0])
    import signal

    old = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *a: None)  # the child handles Ctrl-C itself
    try:
        rc = proc.wait()
    finally:
        signal.signal(signal.SIGINT, old)
        watcher.stop()
        if thread:
            thread.join()
    end_meta, end_files, end_head = repo.capture(message="after " + name, trigger="run-end", session=sid)
    sess = repo.get_session(sid)
    sess.update({"ended": time.time(), "exit_code": rc, "end_snapshot": end_meta["id"] if end_meta else end_head})
    repo.save_session(sess)
    start_files = repo.snapshot_files(sess["start_snapshot"])
    stats = summarize(diff_manifests(start_files, end_files))
    changed = sum(stats.values())
    sys.stderr.write("%s session %s finished in %s, exit %d: %s. %s\n" % (
        term.cyan("rewind:"), term.bold(sid), term.duration(time.time() - started), rc,
        term.stats_str(stats) if changed else "no file changes",
        ("Undo with `rewind undo --session`." if changed else "")))
    return rc


def cmd_hook(args):
    from . import hooks

    if args.action == "exec":
        if len(args.rest) < 2:
            raise RewindError("usage: rewind hook exec <agent> <event>")
        code, out, errtext = hooks.run_hook(args.rest[0], args.rest[1], sys.stdin.read() if not sys.stdin.isatty() else "")
        if out:
            sys.stdout.write(out + ("\n" if not out.endswith("\n") else ""))
        if errtext:
            sys.stderr.write(errtext)
        return code
    repo = _repo()
    if args.action == "status":
        for agent in hooks.AGENTS:
            path = hooks.installed(repo.root, agent)
            state = term.green("installed") if path else term.dim("not installed")
            where = os.path.relpath(str(path), str(repo.root)) if path else ""
            print("  %-8s %s  %s" % (agent, state, term.dim(where)))
        return 0
    agents = args.rest or []
    if not agents:
        raise RewindError("which agent? e.g. `rewind hook %s claude` (choices: %s)" % (args.action, ", ".join(hooks.AGENTS)))
    if agents == ["all"]:
        agents = list(hooks.AGENTS)
    for agent in agents:
        if args.action == "install":
            path = hooks.install(repo.root, agent, local=args.local)
            print("%s %s hooks written to %s" % (term.green(_check()), agent, os.path.relpath(str(path), str(repo.root))))
            if agent == "codex":
                print(term.dim("  Codex asks you to review new hooks: open /hooks in Codex and trust them."))
        elif args.action == "uninstall":
            path = hooks.uninstall(repo.root, agent, local=args.local)
            print("Removed %s hooks%s" % (agent, (" from " + os.path.relpath(str(path), str(repo.root))) if path else " (none found)"))
        else:
            raise RewindError("unknown action %r" % args.action)
    return 0


def cmd_guard(args):
    from . import guard

    if args.action == "rules":
        try:
            disabled = set(_repo().config.get("guard.disable") or [])
        except NotInitialized:
            disabled = set()
        for rule, (sev, desc) in sorted(guard.RULES.items()):
            state = term.dim("disabled") if rule in disabled else (term.red(sev) if sev == "critical" else term.yellow(sev))
            print("  %-22s %-9s %s" % (rule, term.pad(state, 9), desc))
        return 0
    if args.action == "allow":
        if not args.text:
            raise RewindError("usage: rewind guard allow '<glob pattern matching the whole command>'")
        repo = _repo()
        pat = " ".join(args.text)
        repo.config.append("guard.allow", pat)
        print("%s Allowed: %s" % (term.green(_check()), pat))
        return 0
    if args.action == "check":
        if not args.text:
            raise RewindError('usage: rewind guard check "<command>"')
        command = " ".join(args.text)
        try:
            repo = _repo()
            cfg, root = repo.config, str(repo.root)
        except NotInitialized:
            from .config import Config
            from pathlib import Path

            cfg, root = Config(Path(".rewind-defaults-only", "none.json")), os.getcwd()
        findings, decision = guard.evaluate(command, cfg, cwd=os.getcwd(), root=root)
        if not findings:
            print("%s looks safe" % term.green(_check()))
            return 0
        verdict = {"block": term.bold_red("BLOCK"), "snapshot": term.bold_yellow("SNAPSHOT"), "warn": term.bold_yellow("WARN")}[decision]
        print("%s  %s" % (verdict, command))
        for f in findings:
            sev = term.red(f.severity) if f.severity == "critical" else term.yellow(f.severity)
            print("  %s %s  %s" % (term.pad(sev, 8), f.rule, term.dim(f.message())))
        return 2 if decision == "block" else 1
    raise RewindError("unknown guard action")


TRIGGER_LABELS = {
    "manual": ("snap", term.green),
    "watch": ("watch", term.blue),
    "hook": (None, term.magenta),
    "guard": ("seatbelt", term.bold_yellow),
    "safety": ("safety", term.gray),
    "restore": ("restore", term.cyan),
    "run-start": ("start", term.cyan),
    "run-end": ("end", term.cyan),
    "init": ("init", term.green),
}


def _label(meta):
    label, color = TRIGGER_LABELS.get(meta.get("trigger"), (meta.get("trigger") or "?", term.dim))
    if label is None:
        label = meta.get("tool") or "hook"
    return color(label), len(label)


def _describe(meta):
    msg = meta.get("message") or ""
    detail = meta.get("detail") or ""
    if meta.get("trigger") == "manual":
        return '"%s"' % msg if msg else "checkpoint"
    if meta.get("trigger") in ("hook", "guard") and detail:
        prefix = msg.split(" ")[0] if msg else ""
        if meta.get("trigger") == "guard":
            return "%s: %s" % (msg.split(":")[0], " ".join(detail.split())) if msg.startswith("blocked") else msg
        return "%s %s" % (prefix, " ".join(detail.split()))
    return msg


def _session_header(sess, now):
    bits = [term.bold_cyan("● %s" % sess["id"]), term.bold(sess.get("name") or "")]
    if sess.get("command") and sess.get("command") != sess.get("name"):
        bits.append(term.dim(sess["command"]))
    meta = [term.ago(sess["started"], now)]
    end = sess.get("ended") or sess.get("last")
    if end:
        meta.append(term.duration(end - sess["started"]))
    if sess.get("exit_code") is not None:
        meta.append(("exit %d" % sess["exit_code"]) if sess["exit_code"] == 0 else term.red("exit %d" % sess["exit_code"]))
    elif not sess.get("ended") and sess.get("external"):
        meta.append("hooks")
    return "  ".join(bits) + "  " + term.dim(" · ".join(meta))


def cmd_log(args):
    import shutil

    repo = _repo()
    snaps = repo.list_snapshots()
    if args.session:
        sid = args.session if args.session.startswith("s") else "s" + args.session
        snaps = [m for m in snaps if m.get("session") == sid]
    if not args.all:
        snaps = [m for m in snaps if m.get("trigger") != "safety" or args.verbose]
    if not snaps:
        print("No snapshots yet. Take one with `rewind snap`.")
        return 0
    total = len(snaps)
    snaps = snaps[-args.limit:] if args.limit else snaps
    sessions = {s["id"]: s for s in repo.list_sessions()}
    width = max(60, min(shutil.get_terminal_size((100, 24)).columns, 140))
    now = time.time()
    idw = len(str(snaps[-1]["id"])) + 1
    current_group = object()
    out = []
    for meta in reversed(snaps):
        group = meta.get("session")
        if group != current_group:
            if out:
                out.append("")
            if group and group in sessions:
                out.append(_session_header(sessions[group], now))
            current_group = group
        label, llen = _label(meta)
        stats = term.stats_str(meta.get("stats"))
        prefix = "  │ " if group else "  ○ "
        left = "%s%s  %s  %s" % (
            term.dim(prefix) if group else term.dim(prefix),
            term.bold(("#%d" % meta["id"]).rjust(idw)),
            term.dim(term.ago(meta["time"], now).ljust(8)),
            term.pad(label, 9),
        )
        desc = _describe(meta)
        room = width - term.visible_len(left) - term.visible_len(stats) - 3
        if room > 8 and desc:
            desc = desc if len(desc) <= room else desc[:room - 1] + "…"
        else:
            desc = ""
        out.append("%s %s%s%s" % (left, desc, " " * max(1, room - len(desc) + 1), stats))
        if args.files:
            for change in meta.get("changes", [])[:20]:
                status, path = change[0], change[1]
                color = {"A": term.green, "M": term.yellow, "D": term.red, "R": term.cyan}[status]
                extra = (" ← " + change[2]) if len(change) > 2 else ""
                out.append("  %s       %s %s%s" % (term.dim("│") if group else " ", color(status), path, term.dim(extra)))
            if meta.get("nchanges", 0) > 20:
                out.append("  %s       %s" % (term.dim("│") if group else " ", term.dim("… %d more" % (meta["nchanges"] - 20))))
    print("\n".join(out))
    if total > len(snaps):
        print(term.dim("\n  showing %d of %d snapshots (use -n 0 for all)" % (len(snaps), total)))
    return 0


def _print_changes(changes):
    for c in changes:
        color = {"A": term.green, "M": term.yellow, "D": term.red, "R": term.cyan}[c["status"]]
        extra = term.dim("  (from %s)" % c["old_path"]) if c.get("old_path") else ""
        print("  %s %s%s" % (color(c["status"]), c["path"], extra))


def cmd_show(args):
    from .diff import colorize, diff_manifests, unified

    repo = _repo()
    sid = repo.resolve(args.ref)
    meta = repo.get_snapshot(sid)
    label, _ = _label(meta)
    print("%s  %s  %s" % (term.bold("#%d" % sid), label, term.dim(term.clock(meta["time"]) + " (" + term.ago(meta["time"]) + ")")))
    if meta.get("message"):
        print("  message  %s" % meta["message"])
    if meta.get("detail"):
        print("  detail   %s" % meta["detail"])
    if meta.get("session"):
        print("  session  %s" % meta["session"])
    print("  files    %d (%s)" % (meta.get("files", 0), term.size(meta.get("bytes", 0))))
    parent = meta.get("parent")
    old = repo.snapshot_files(parent) if parent is not None and repo.snap_path(parent).exists() else {}
    changes = diff_manifests(old, repo.snapshot_files(sid))
    print("  changes  %s%s" % (term.stats_str(meta.get("stats")), term.dim("  vs #%s" % parent) if parent else ""))
    print()
    _print_changes(changes)
    if args.patch:
        for c in changes:
            print()
            print("\n".join(colorize(unified(repo, c))))
    return 0


def _looks_like_ref(tok):
    t = tok.lstrip("#").lower()
    return t.isdigit() or t in ("head", "latest", "last") or t.startswith(("head~", "latest~", "last~"))


def cmd_diff(args):
    from .diff import colorize, diff_manifests, summarize, unified
    from .restore import make_scope
    from .scan import scan

    repo = _repo()
    refs, paths = [], []
    for tok in args.items:
        if len(refs) < 2 and not paths and _looks_like_ref(tok):
            refs.append(tok)
        else:
            paths.append(tok)
    if len(refs) == 0:
        a_id = repo.resolve("HEAD")
        a, b, b_label = repo.snapshot_files(a_id), scan(repo).files, "working tree"
    elif len(refs) == 1:
        a_id = repo.resolve(refs[0])
        a, b, b_label = repo.snapshot_files(a_id), scan(repo).files, "working tree"
    else:
        a_id, b_id = repo.resolve(refs[0]), repo.resolve(refs[1])
        a, b, b_label = repo.snapshot_files(a_id), repo.snapshot_files(b_id), "#%d" % b_id
    scope = make_scope([repo.rel(p) for p in paths] if paths else None)
    changes = [c for c in diff_manifests(a, b) if scope is None or scope(c["path"]) or (c.get("old_path") and scope(c["old_path"]))]
    if not changes:
        print("No differences between #%d and %s." % (a_id, b_label))
        return 0
    print(term.dim("#%d → %s   %s" % (a_id, b_label, term.stats_str(summarize(changes)))))
    if args.stat:
        _print_changes(changes)
        return 0
    for c in changes:
        print()
        print("\n".join(colorize(unified(repo, c, context=args.context))))
    return 0


def cmd_undo(args):
    from . import restore

    repo = _repo()
    if args.session is not None:
        sid = None if args.session == "__latest__" else args.session
        sess, preview = restore.undo_session(repo, sid, dry_run=True)
        if not preview.plan:
            print("Nothing to undo: files already match the start of session %s." % sess["id"])
            return 0
        title = "Undo session %s (%s, %s) -> back to #%d:" % (
            sess["id"], sess.get("name") or "", term.ago(sess["started"]), sess["start_snapshot"])
        return _run_restore_flow(lambda: preview, lambda: restore.undo_session(repo, sess["id"])[1], title, args,
                                 done="Session %s undone, back to #%d" % (sess["id"], sess["start_snapshot"]))
    preview = restore.undo(repo, dry_run=True, paths=args.paths)
    if not preview.plan:
        print("Nothing to undo.")
        return 0
    target = preview.plan.target_id
    meta = repo.get_snapshot(target)
    title = "Undo -> back to #%d (%s, %s):" % (target, _describe(meta) or meta.get("trigger"), term.ago(meta["time"]))
    return _run_restore_flow(lambda: preview, lambda: restore.undo(repo, paths=args.paths), title, args,
                             done="Undone, back to #%d" % target, redo=True)


def cmd_redo(args):
    from . import restore

    repo = _repo()
    preview = restore.redo(repo, dry_run=True)
    title = "Redo -> forward to #%d:" % preview.plan.target_id if preview.plan else "Redo:"
    return _run_restore_flow(lambda: preview, lambda: restore.redo(repo), title, args, "Nothing to redo.",
                             done="Redone, forward to #%s" % preview.plan.target_id)


def cmd_restore(args):
    from . import restore

    repo = _repo()
    sid = repo.resolve(args.ref)
    meta = repo.get_snapshot(sid)
    scope = (" " + ", ".join(args.paths)) if args.paths else ""
    title = "Restore%s to #%d (%s):" % (scope, sid, term.ago(meta["time"]))
    preview = restore.restore(repo, sid, args.paths, dry_run=True, delete=not args.keep_new)
    return _run_restore_flow(lambda: preview, lambda: restore.restore(repo, sid, args.paths, delete=not args.keep_new), title, args)


def cmd_back(args):
    from . import restore
    from .timeparse import TimeParseError, parse_point

    repo = _repo()
    when = " ".join(args.when)
    try:
        ts = parse_point(when)
    except TimeParseError as exc:
        raise RewindError(str(exc))
    meta, preview = restore.back(repo, ts, args.paths, dry_run=True)
    title = "Back to #%d from %s (%s):" % (meta["id"], term.clock(meta["time"]), term.ago(meta["time"]))
    return _run_restore_flow(lambda: preview, lambda: restore.restore(repo, meta["id"], args.paths), title, args)


def cmd_recover(args):
    from . import restore

    repo = _repo()
    preview = restore.recover(repo, args.patterns, from_ref=args.source, dry_run=True)
    if not preview.plan:
        print("Nothing to recover: no snapshot has a deleted file matching %s." % " ".join(args.patterns))
        return 0
    title = "Recover %d deleted file(s):" % len(preview.plan.writes)
    return _run_restore_flow(lambda: preview, lambda: restore.recover(repo, args.patterns, from_ref=args.source), title, args)


def cmd_ui(args):
    from .ui import serve

    return serve(_repo(), port=args.port, open_browser=not args.no_open)


def cmd_gc(args):
    from .gc import run_gc

    repo = _repo()
    res = run_gc(repo, dry_run=args.dry_run)
    verb = "Would delete" if args.dry_run else "Deleted"
    print("%s %d of %d snapshots and %d objects, %s %s." % (
        verb, res["snapshots_deleted"], res["snapshots_total"], res["objects_deleted"],
        "freeing" if not args.dry_run else "would free", term.size(res["bytes_freed"])))
    return 0


def cmd_status(args):
    from .diff import diff_manifests, summarize
    from .hooks import AGENTS, installed
    from .scan import scan
    from .watch import daemon_pid

    repo = _repo()
    ids = repo.snapshot_ids()
    count, nbytes = repo.store_size()
    print("%s  %s" % (term.bold("project"), repo.root))
    print("%s    %s%s" % (term.bold("store"), repo.store, term.dim("  (global)") if repo.is_global else ""))
    print("%s     %d objects, %s" % (term.bold("size"), count, term.size(nbytes)))
    print("%s %d snapshots, %d sessions" % (term.bold("history"), len(ids), len(repo.list_sessions())))
    pid = daemon_pid(repo)
    print("%s  %s" % (term.bold("watcher"), term.green("running (pid %d)" % pid) if pid else term.dim("off")))
    hooks = [a for a in AGENTS if installed(repo.root, a)]
    print("%s    %s" % (term.bold("hooks"), ", ".join(hooks) if hooks else term.dim("none")))
    if not ids:
        return 0
    head = repo.get_snapshot(ids[-1])
    print("%s   #%d  %s" % (term.bold("latest"), head["id"], term.dim(term.ago(head["time"]))))
    changes = diff_manifests(repo.snapshot_files(head), scan(repo).files)
    print()
    if not changes:
        print("Working tree matches #%d." % head["id"])
    else:
        print("Changes since #%d  %s" % (head["id"], term.stats_str(summarize(changes))))
        _print_changes(changes[:30])
        if len(changes) > 30:
            print(term.dim("  … and %d more" % (len(changes) - 30)))
    return 0


def cmd_config(args):
    import json

    from .config import DESCRIPTIONS

    repo = _repo()
    cfg = repo.config
    if args.action in (None, "list"):
        for key, value, default in cfg.flat():
            v = json.dumps(value)
            mark = "" if value == default else term.yellow("  (changed)")
            print("  %-20s %s%s" % (key, term.bold(v), mark))
            print("  %-20s %s" % ("", term.dim(DESCRIPTIONS[key])))
        print(term.dim("\n  file: %s" % cfg.path))
        return 0
    if args.action == "path":
        print(cfg.path)
        return 0
    if args.action == "get":
        if not args.key:
            raise RewindError("usage: rewind config get <key>")
        value = cfg.get(args.key)
        if value is None and args.key not in DESCRIPTIONS:
            raise RewindError("unknown config key %r" % args.key)
        print(json.dumps(value) if not isinstance(value, str) else value)
        return 0
    if args.action == "set":
        if not args.key or args.value is None:
            raise RewindError("usage: rewind config set <key> <value>")
        cfg.set(args.key, args.value)
        print("%s %s = %s" % (term.green(_check()), args.key, json.dumps(cfg.get(args.key))))
        return 0
    if args.action == "unset":
        cfg.unset(args.key)
        print("%s %s reset to default" % (term.green(_check()), args.key))
        return 0
    raise RewindError("unknown config action %r" % args.action)


def cmd_doctor(args):
    import hashlib
    import shutil
    import zlib

    from .hooks import AGENTS, installed
    from .scan import scan
    from .watch import daemon_pid

    problems = 0

    def ok(msg):
        print("  %s %s" % (term.green(_check()), msg))

    def bad(msg):
        nonlocal problems
        problems += 1
        print("  %s %s" % (term.red("✗" if _check() == "✓" else "x"), msg))

    def note(msg):
        print("  %s %s" % (term.yellow("!"), msg))

    print(term.bold("rewind %s on Python %s" % (__version__, sys.version.split()[0])))
    if sys.version_info < (3, 9):
        bad("Python 3.9+ is required")
    if shutil.which("rewind"):
        ok("`rewind` is on PATH (%s)" % shutil.which("rewind"))
    else:
        note("`rewind` is not on PATH; hooks will call %s -m rewind_cli" % sys.executable)
    try:
        repo = _repo()
    except NotInitialized:
        note("this directory is not a rewind project (run `rewind init`)")
        return 1 if problems else 0
    ok("project %s" % repo.root)
    probe = repo.store / ".doctor-probe"
    try:
        probe.write_text("x")
        probe.unlink()
        ok("store is writable (%s)" % repo.store)
    except OSError as exc:
        bad("store is not writable: %s" % exc)
    if (repo.store / "lock").exists():
        note("a lock file exists (another rewind process may be running)")
    t0 = time.time()
    res = scan(repo)
    ok("scanned %d files in %.0f ms (%d re-hashed)" % (len(res.files), (time.time() - t0) * 1000, res.hashed))
    if res.skipped:
        note("%d file(s) are over the %s MB limit and are NOT protected:" % (len(res.skipped), repo.config.get("max_file_size_mb")))
        for path, sz in res.skipped[:10]:
            print("      %s (%s)" % (path, term.size(sz)))
        print(term.dim("      raise it with: rewind config set max_file_size_mb 200"))
    for path, msg in res.errors[:5]:
        note("could not read %s: %s" % (path, msg))
    if not repo.config.get("include_git", True) and (repo.root / ".git").exists():
        note(".git is excluded from snapshots (include_git=false)")
    elif (repo.root / ".git").exists():
        ok(".git directory is included in snapshots")
    ids = repo.snapshot_ids()
    check_ids = ids if args.full else ids[-1:]
    missing = corrupt = checked = 0
    seen = set()
    for sid in check_ids:
        meta = repo.get_snapshot(sid)
        try:
            files = repo.load_manifest(meta["manifest"])
        except Exception:
            bad("snapshot #%d: manifest is missing or unreadable" % sid)
            continue
        for entry in files.values():
            sha = entry.get("h")
            if not sha or sha in seen:
                continue
            seen.add(sha)
            path = repo.object_path(sha)
            if not path.exists():
                missing += 1
                continue
            if args.full:
                try:
                    if hashlib.sha256(zlib.decompress(path.read_bytes())).hexdigest() != sha:
                        corrupt += 1
                except Exception:
                    corrupt += 1
            checked += 1
    scope = "all snapshots" if args.full else ("latest snapshot" if ids else "no snapshots")
    if missing or corrupt:
        bad("%s: %d missing and %d corrupt objects" % (scope, missing, corrupt))
    else:
        ok("%s: %d objects present%s" % (scope, checked, " and verified" if args.full else ""))
    hooks = [a for a in AGENTS if installed(repo.root, a)]
    if hooks:
        ok("hooks installed: %s" % ", ".join(hooks))
    else:
        note("no agent hooks installed (use `rewind run -- <agent>` or `rewind hook install claude`)")
    errlog = repo.store / "hook-errors.log"
    if errlog.exists() and errlog.stat().st_size:
        note("hook errors were logged to %s" % errlog)
    pid = daemon_pid(repo)
    ok("watcher running (pid %d)" % pid) if pid else note("watcher not running (optional: `rewind watch --daemon`)")
    print()
    print(term.green("No problems found.") if not problems else term.red("%d problem(s) found." % problems))
    return 1 if problems else 0


# ------------------------------------------------------------------ parser
class _Fmt(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog):
        super().__init__(prog, max_help_position=30)


def _sub(subs, name, help_, epilog=None, **kw):
    p = subs.add_parser(name, help=help_, description=help_, epilog=("examples:\n" + epilog) if epilog else None,
                        formatter_class=_Fmt, **kw)
    return p


def _restore_flags(p):
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    p.add_argument("-n", "--dry-run", action="store_true", help="show what would change, change nothing")


def build_parser():
    parser = argparse.ArgumentParser(prog="rewind", add_help=False)
    parser.add_argument("--version", action="version", version="rewind %s" % __version__)
    subs = parser.add_subparsers(dest="cmd", metavar="<command>")

    p = _sub(subs, "init", "Set up Rewind in this directory", "  rewind init\n  rewind init --hooks claude -y\n  rewind init --global   # keep the store in ~/.rewind instead of ./.rewind")
    p.add_argument("path", nargs="?", help="project directory (default: current)")
    p.add_argument("--global", dest="global_store", action="store_true", help="keep the store in ~/.rewind/projects/<hash>")
    p.add_argument("--hooks", metavar="AGENTS", help="comma-separated agents to install hooks for")
    p.add_argument("--no-hooks", action="store_true", help="don't offer to install hooks")
    p.add_argument("-y", "--yes", action="store_true", help="install hooks for every detected agent")
    p.set_defaults(func=cmd_init)

    p = _sub(subs, "snap", "Take a snapshot now (no-op if nothing changed)", '  rewind snap -m "before refactor"')
    p.add_argument("-m", "--message", help="label for this snapshot")
    p.add_argument("-f", "--force", action="store_true", help="record a snapshot even if nothing changed")
    p.add_argument("-q", "--quiet", action="store_true", help="print only the new snapshot id")
    p.set_defaults(func=cmd_snap)

    p = _sub(subs, "watch", "Auto-snapshot as files change", "  rewind watch            # foreground\n  rewind watch --daemon   # background\n  rewind watch status\n  rewind watch stop")
    p.add_argument("action", nargs="?", choices=["start", "stop", "status"], help="manage the background watcher")
    p.add_argument("-d", "--daemon", action="store_true", help="run in the background")
    p.add_argument("--foreground", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--interval", type=float, help="seconds between polls (default 1)")
    p.add_argument("--debounce", type=float, help="seconds of quiet before snapshotting (default 2)")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(func=cmd_watch)

    p = _sub(subs, "run", "Run a command (an agent) inside a recorded session", "  rewind run -- claude\n  rewind run --name refactor -- codex exec 'refactor utils'\n  rewind run -- aider --model sonnet")
    p.add_argument("--name", help="session name (default: the command)")
    p.add_argument("--no-watch", action="store_true", help="only checkpoint before and after")
    p.add_argument("command", nargs=argparse.REMAINDER, help="the command to run, after --")
    p.set_defaults(func=cmd_run)

    p = _sub(subs, "hook", "Install agent hooks (claude, codex, gemini, cursor)", "  rewind hook install claude\n  rewind hook install claude --local   # .claude/settings.local.json\n  rewind hook status\n  rewind hook uninstall cursor")
    p.add_argument("action", choices=["install", "uninstall", "status", "exec"])
    p.add_argument("rest", nargs="*", help="agent name(s), or 'all'")
    p.add_argument("--local", action="store_true", help="Claude Code: write .claude/settings.local.json")
    p.set_defaults(func=cmd_hook)

    p = _sub(subs, "guard", "The seatbelt: detect destructive commands", "  rewind guard check \"git reset --hard\"\n  rewind guard allow \"git clean -fdx build/*\"\n  rewind guard rules")
    p.add_argument("action", choices=["check", "allow", "rules"])
    p.add_argument("text", nargs="*", help="command or pattern")
    p.set_defaults(func=cmd_guard)

    p = _sub(subs, "log", "Show the timeline of snapshots, grouped by session", "  rewind log\n  rewind log --files -n 10\n  rewind log --session s3")
    p.add_argument("-n", "--limit", type=int, default=30, help="how many snapshots (0 = all)")
    p.add_argument("--files", action="store_true", help="list changed files under each snapshot")
    p.add_argument("--session", help="only this session")
    p.add_argument("-a", "--all", action="store_true", help="include safety snapshots")
    p.add_argument("-v", "--verbose", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_log)

    p = _sub(subs, "show", "Show what a snapshot changed", "  rewind show 12\n  rewind show HEAD -p")
    p.add_argument("ref", nargs="?", default="HEAD")
    p.add_argument("-p", "--patch", action="store_true", help="include diffs")
    p.set_defaults(func=cmd_show)

    p = _sub(subs, "diff", "Diff snapshots (or a snapshot against your files)", "  rewind diff              # latest snapshot -> working tree\n  rewind diff 12           # #12 -> working tree\n  rewind diff 12 15 src/   # #12 -> #15, only src/")
    p.add_argument("items", nargs="*", metavar="ref|path")
    p.add_argument("--stat", action="store_true", help="only list changed files")
    p.add_argument("-U", "--context", type=int, default=3, help="lines of context")
    p.set_defaults(func=cmd_diff)

    p = _sub(subs, "undo", "Revert the last change, or a whole agent session", "  rewind undo\n  rewind undo --session        # the latest session\n  rewind undo --session s4 -y\n  rewind undo src/app.py       # only this file")
    p.add_argument("paths", nargs="*", help="limit the undo to these paths")
    p.add_argument("-s", "--session", nargs="?", const="__latest__", help="undo an entire session (default: latest)")
    _restore_flags(p)
    p.set_defaults(func=cmd_undo)

    p = _sub(subs, "redo", "Re-apply what the last undo reverted")
    _restore_flags(p)
    p.set_defaults(func=cmd_redo)

    p = _sub(subs, "restore", "Restore the project (or some paths) to a snapshot", "  rewind restore 12\n  rewind restore 12 src/app.py 'docs/*.md'\n  rewind restore HEAD~3 --dry-run")
    p.add_argument("ref")
    p.add_argument("paths", nargs="*")
    p.add_argument("--keep-new", action="store_true", help="don't delete files that were created after the snapshot")
    _restore_flags(p)
    p.set_defaults(func=cmd_restore)

    p = _sub(subs, "back", "Go back in time", "  rewind back 10m\n  rewind back \"2 hours\"\n  rewind back 14:30 src/")
    p.add_argument("when", nargs="+", help="duration (10m, 2h, '2 hours') or time (14:30, 2026-10-10 09:00)")
    p.add_argument("--paths", nargs="*", help="limit to these paths")
    _restore_flags(p)
    p.set_defaults(func=cmd_back)

    p = _sub(subs, "recover", "Bring back deleted files from the latest snapshot that had them", "  rewind recover .env\n  rewind recover 'src/**/*.py'\n  rewind recover data/ --from 40")
    p.add_argument("patterns", nargs="+", metavar="path|glob")
    p.add_argument("--from", dest="source", help="recover from this snapshot only")
    _restore_flags(p)
    p.set_defaults(func=cmd_recover)

    p = _sub(subs, "ui", "Open the local web UI", "  rewind ui\n  rewind ui --port 7777 --no-open")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--no-open", action="store_true", help="don't open a browser")
    p.set_defaults(func=cmd_ui)

    p = _sub(subs, "gc", "Apply the retention policy and delete unreferenced data", "  rewind gc --dry-run")
    p.add_argument("-n", "--dry-run", action="store_true")
    p.set_defaults(func=cmd_gc)

    p = _sub(subs, "status", "Show store info and changes since the latest snapshot")
    p.set_defaults(func=cmd_status)

    p = _sub(subs, "config", "Show or change settings", "  rewind config\n  rewind config set guard.mode snapshot\n  rewind config set max_file_size_mb 200\n  rewind config get gc.daily_days")
    p.add_argument("action", nargs="?", choices=["list", "get", "set", "unset", "path"])
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.set_defaults(func=cmd_config)

    p = _sub(subs, "doctor", "Check the installation, store integrity and integrations")
    p.add_argument("--full", action="store_true", help="verify every object of every snapshot")
    p.set_defaults(func=cmd_doctor)
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        if len(argv) > 1 and argv[0] == "help":
            argv = [argv[1], "--help"]
        else:
            sys.stdout.write(_main_help())
            return 0
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    if not getattr(args, "func", None):
        sys.stdout.write(_main_help())
        return 1
    try:
        return args.func(args) or 0
    except RewindError as exc:
        term.err(str(exc))
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        return 130
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0
    except Exception as exc:
        from .config import ConfigError

        if isinstance(exc, ConfigError):
            term.err(str(exc))
            return 1
        if os.environ.get("REWIND_DEBUG"):
            raise
        term.err("unexpected %s: %s (set REWIND_DEBUG=1 for a traceback, and please report it)" % (type(exc).__name__, exc))
        return 1
