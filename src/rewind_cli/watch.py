"""Polling file watcher (stdlib only) with debounce, plus daemon management."""

import os
import signal
import subprocess
import sys
import threading
import time

from .scan import fingerprint, load_ignore
from .store import RewindError


class Watcher:
    """Polls a stat fingerprint of the tree; snapshots once changes settle.

    Only ``stat`` calls happen on each poll -- files are read and hashed only
    when a snapshot is actually taken, and then only the changed ones.
    """

    def __init__(self, repo, interval=None, debounce=None, max_wait=None, session=None, on_snapshot=None, trigger="watch"):
        cfg = repo.config
        self.repo = repo
        self.interval = float(interval if interval is not None else cfg.get("watch.interval", 1.0))
        self.debounce = float(debounce if debounce is not None else cfg.get("watch.debounce", 2.0))
        self.max_wait = float(max_wait if max_wait is not None else cfg.get("watch.max_wait", 30.0))
        self.session = session
        self.on_snapshot = on_snapshot
        self.trigger = trigger
        self.stop_event = threading.Event()
        self.snapshots = []

    def poll_once(self, state):
        now = time.time()
        fp = fingerprint(self.repo, state["matcher"])
        if fp != state["fp"]:
            state["fp"] = fp
            state["last_change"] = now
            if state["first_change"] is None:
                state["first_change"] = now
        if state["first_change"] is not None:
            quiet = now - state["last_change"] >= self.debounce
            overdue = now - state["first_change"] >= self.max_wait
            if quiet or overdue:
                self.snap()
                state["first_change"] = None
                state["fp"] = fingerprint(self.repo, state["matcher"])

    def snap(self, message="auto-snapshot"):
        try:
            meta, _files, _head = self.repo.capture(message=message, trigger=self.trigger, session=self.session)
        except RewindError as exc:
            sys.stderr.write("rewind watch: %s\n" % exc)
            return None
        if meta:
            self.snapshots.append(meta["id"])
            if self.on_snapshot:
                self.on_snapshot(meta)
        return meta

    def run(self):
        state = {"matcher": load_ignore(self.repo), "fp": None, "last_change": 0.0, "first_change": None}
        state["fp"] = fingerprint(self.repo, state["matcher"])
        ignore_file = self.repo.root / ".rewindignore"
        ignore_mtime = ignore_file.stat().st_mtime if ignore_file.exists() else None
        while not self.stop_event.is_set():
            started = time.time()
            try:
                cur = ignore_file.stat().st_mtime if ignore_file.exists() else None
                if cur != ignore_mtime:
                    ignore_mtime = cur
                    state["matcher"] = load_ignore(self.repo)
                self.poll_once(state)
            except Exception as exc:  # keep watching through transient errors
                sys.stderr.write("rewind watch: %s\n" % exc)
            elapsed = time.time() - started
            # adaptive: never spend more than ~20% of wall time scanning
            self.stop_event.wait(max(self.interval, elapsed * 4))
        if state["first_change"] is not None:
            self.snap()

    def start_thread(self):
        t = threading.Thread(target=self.run, name="rewind-watch", daemon=True)
        t.start()
        return t

    def stop(self):
        self.stop_event.set()


# ------------------------------------------------------------------ daemon
def pid_file(repo):
    return repo.store / "watch.pid"


def daemon_pid(repo):
    from .store import _pid_alive

    try:
        pid = int(pid_file(repo).read_text().strip())
    except (OSError, ValueError):
        return None
    if _pid_alive(pid):
        return pid
    try:
        pid_file(repo).unlink()
    except OSError:
        pass
    return None


def start_daemon(repo):
    if daemon_pid(repo):
        raise RewindError("watcher already running (pid %d)" % daemon_pid(repo))
    log = open(str(repo.store / "watch.log"), "ab")
    cmd = [sys.executable, "-m", "rewind_cli", "watch", "--foreground", "--quiet"]
    kwargs = dict(cwd=str(repo.root), stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    pid_file(repo).write_text(str(proc.pid))
    return proc.pid


def stop_daemon(repo, timeout=5.0):
    pid = daemon_pid(repo)
    if not pid:
        return None
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.time() + timeout
    from .store import _pid_alive

    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.05)
        try:  # reap if it is our child (tests)
            os.waitpid(pid, os.WNOHANG)
        except (OSError, AttributeError):
            pass
    try:
        pid_file(repo).unlink()
    except OSError:
        pass
    return pid
