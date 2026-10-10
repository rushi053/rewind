import os
import shutil
import time

import pytest

from conftest import read, write
from rewind_cli import restore
from rewind_cli.store import RewindError, write_json


def test_undo_unsnapshotted_change_goes_to_latest(project):
    project.capture()
    write(project, "src/app.py", "oops\n")
    res = restore.undo(project)
    assert res.plan.target_id == 1
    assert read(project, "src/app.py") == "print('hello')\n"


def test_repeated_undo_walks_back_and_redo_walks_forward(project):
    project.capture()                       # 1: v0
    for v in ("v1", "v2", "v3"):
        write(project, "src/app.py", v)
        project.capture()                   # 2, 3, 4
    restore.undo(project)
    assert read(project, "src/app.py") == "v2"
    restore.undo(project)
    assert read(project, "src/app.py") == "v1"
    restore.undo(project)
    assert read(project, "src/app.py") == "print('hello')\n"
    with pytest.raises(RewindError):
        restore.undo(project)
    restore.redo(project)
    assert read(project, "src/app.py") == "v1"
    restore.redo(project)
    assert read(project, "src/app.py") == "v2"
    restore.redo(project)
    assert read(project, "src/app.py") == "v3"
    with pytest.raises(RewindError):
        restore.redo(project)


def test_new_change_after_undo_resets_the_cursor(project):
    project.capture()
    write(project, "src/app.py", "v1")
    project.capture()
    restore.undo(project)
    write(project, "src/app.py", "fresh")
    with pytest.raises(RewindError):
        restore.redo(project)
    restore.undo(project)  # undoes "fresh", back to the last snapshot that differs
    assert read(project, "src/app.py") != "fresh"


def test_undo_limited_to_paths(project):
    project.capture()
    write(project, "src/app.py", "x")
    write(project, "README.md", "y")
    restore.undo(project, paths=["README.md"])
    assert read(project, "README.md") == "# demo\n"
    assert read(project, "src/app.py") == "x"


def test_undo_session_restores_everything(project):
    write(project, ".git/HEAD", "ref: refs/heads/main\n")
    project.capture()
    sess = project.new_session("agent")
    meta, _, head = project.capture(trigger="run-start", session=sess["id"])
    s = project.get_session(sess["id"])
    s["start_snapshot"] = head
    project.save_session(s)
    # the agent goes wild across several snapshots
    write(project, "src/app.py", "rewritten")
    project.capture(trigger="hook", session=sess["id"])
    shutil.rmtree(str(project.root / ".git"))
    os.remove(str(project.root / ".env"))
    write(project, "trash.txt", "x")
    project.capture(trigger="hook", session=sess["id"])
    s2, res = restore.undo_session(project)
    assert s2["id"] == sess["id"]
    assert read(project, "src/app.py") == "print('hello')\n"
    assert read(project, ".env") == "SECRET=abc\n"
    assert read(project, ".git/HEAD") == "ref: refs/heads/main\n"
    assert not (project.root / "trash.txt").exists()


def test_undo_session_without_sessions(project):
    project.capture()
    with pytest.raises(RewindError):
        restore.undo_session(project)


def test_recover_deleted_file_from_latest_snapshot_that_had_it(project):
    write(project, "notes.txt", "v1")
    project.capture()
    write(project, "notes.txt", "v2")
    project.capture()
    os.remove(str(project.root / "notes.txt"))
    project.capture()
    write(project, "src/app.py", "unrelated edit")
    res = restore.recover(project, ["notes.txt"])
    assert read(project, "notes.txt") == "v2"
    assert read(project, "src/app.py") == "unrelated edit", "recover never touches other files"
    assert res.sources == {"notes.txt": 2}


def test_recover_glob_and_directory(project):
    write(project, "data/a.csv", "a")
    write(project, "data/sub/b.csv", "b")
    write(project, "data/c.json", "c")
    project.capture()
    shutil.rmtree(str(project.root / "data"))
    restore.recover(project, ["data/**/*.csv", "data/*.csv"])
    assert read(project, "data/a.csv") == "a"
    assert read(project, "data/sub/b.csv") == "b"
    assert not (project.root / "data/c.json").exists()
    restore.recover(project, ["data"])
    assert read(project, "data/c.json") == "c"


def test_recover_basename_glob(project):
    project.capture()
    os.remove(str(project.root / ".env"))
    restore.recover(project, [".env*"])
    assert read(project, ".env") == "SECRET=abc\n"


def test_recover_nothing(project):
    project.capture()
    res = restore.recover(project, ["nope.txt"])
    assert not res.plan


def _age(project, sid, seconds_ago):
    meta = project.get_snapshot(sid)
    meta["time"] = time.time() - seconds_ago
    write_json(project.snap_path(sid), meta)


def test_back_in_time(project):
    project.capture()                 # 1, two hours ago
    write(project, "src/app.py", "30 min ago")
    project.capture()                 # 2
    write(project, "src/app.py", "now-ish")
    project.capture()                 # 3
    _age(project, 1, 7200)
    _age(project, 2, 1800)
    _age(project, 3, 60)
    meta, res = restore.back(project, time.time() - 600)
    assert meta["id"] == 2
    assert read(project, "src/app.py") == "30 min ago"
    meta, res = restore.back(project, time.time() - 3 * 3600 + 3600)
    assert meta["id"] == 1
    with pytest.raises(RewindError):
        restore.back(project, time.time() - 10 * 86400)
