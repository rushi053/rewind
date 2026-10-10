import os
import sys
import time

import pytest

from conftest import write
from rewind_cli.scan import load_ignore
from rewind_cli.watch import Watcher, daemon_pid, start_daemon, stop_daemon


def test_watcher_debounces_then_snapshots(project):
    project.capture()
    w = Watcher(project, interval=0.01, debounce=0.2, max_wait=30)
    state = {"matcher": load_ignore(project), "fp": None, "last_change": 0.0, "first_change": None}
    from rewind_cli.scan import fingerprint

    state["fp"] = fingerprint(project, state["matcher"])
    write(project, "src/app.py", "edit 1 with different size")
    w.poll_once(state)
    assert w.snapshots == []  # change seen, waiting for quiet
    write(project, "src/app.py", "edit 2, also a different size!")
    w.poll_once(state)
    assert w.snapshots == []
    time.sleep(0.25)
    w.poll_once(state)
    assert len(w.snapshots) == 1
    meta = project.get_snapshot(w.snapshots[0])
    assert meta["trigger"] == "watch"


def test_watcher_thread_end_to_end(project):
    project.capture()
    w = Watcher(project, interval=0.05, debounce=0.1)
    t = w.start_thread()
    time.sleep(0.15)
    write(project, "new.txt", "hello")
    deadline = time.time() + 5
    while not w.snapshots and time.time() < deadline:
        time.sleep(0.05)
    w.stop()
    t.join(5)
    assert w.snapshots, "watcher should have snapshotted the new file"
    assert "new.txt" in project.snapshot_files(w.snapshots[-1])


def test_watcher_ignores_ignored_paths(project):
    project.capture()
    w = Watcher(project, interval=0.01, debounce=0.0)
    from rewind_cli.scan import fingerprint

    state = {"matcher": load_ignore(project), "fp": None, "last_change": 0.0, "first_change": None}
    state["fp"] = fingerprint(project, state["matcher"])
    write(project, "node_modules/x.js", "x")
    w.poll_once(state)
    w.poll_once(state)
    assert w.snapshots == []


@pytest.mark.skipif(os.name == "nt", reason="daemon test uses POSIX signals")
def test_daemon_start_status_stop(project, monkeypatch):
    project.config.set("watch.interval", "0.1")
    project.config.set("watch.debounce", "0.1")
    project.reload_config()
    pid = start_daemon(project)
    try:
        assert daemon_pid(project) == pid
        time.sleep(0.8)
        write(project, "daemon.txt", "seen by the daemon")
        deadline = time.time() + 10
        found = False
        while time.time() < deadline and not found:
            time.sleep(0.2)
            for sid in project.snapshot_ids():
                if "daemon.txt" in project.snapshot_files(sid):
                    found = True
        assert found, (project.store / "watch.log").read_text()
    finally:
        stop_daemon(project)
    assert daemon_pid(project) is None
