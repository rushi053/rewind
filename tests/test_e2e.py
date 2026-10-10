"""End-to-end: a disastrous agent wrecks a git project; `rewind undo --session` restores it."""

import hashlib
import os
import shutil
import stat
import subprocess
import sys

import pytest

from conftest import run_rewind

pytestmark = pytest.mark.skipif(shutil.which("git") is None or os.name == "nt", reason="needs git and a POSIX shell")


def fingerprint(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir.split(os.sep)[0] == ".rewind":
            continue
        dirnames[:] = [d for d in dirnames if not (rel_dir == "." and d == ".rewind")]
        for name in filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
            p = os.path.join(dirpath, name)
            rel = os.path.normpath(os.path.join(rel_dir, name))
            if os.path.islink(p):
                out[rel] = ("link", os.readlink(p))
            else:
                with open(p, "rb") as fh:
                    out[rel] = (hashlib.sha256(fh.read()).hexdigest(), stat.S_IMODE(os.stat(p).st_mode))
    return out


def git(root, *args):
    subprocess.run(["git"] + list(args), cwd=str(root), check=True, capture_output=True)


EVIL_AGENT = r"""
set -e
sleep 1.2
printf 'garbage\n' > src/math.py
rm -rf docs
rm -rf data
printf 'oops\n' > new-file.txt
chmod 600 deploy.sh
git reset --hard -q
rm -f .git/index .git/refs/heads/main
sleep 0.5
"""


def test_disastrous_agent_session_is_fully_undone(tmp_path):
    root = tmp_path / "victim"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    (root / "src").mkdir()
    (root / "docs").mkdir()
    (root / "data").mkdir()
    (root / "src/math.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "docs/index.md").write_text("# Docs\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    (root / "src/math.py").write_text("def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n")
    (root / "data/.env").write_text("API_KEY=sk-live-secret\n")
    (root / "data/local.db").write_bytes(os.urandom(4096))
    (root / "deploy.sh").write_text("#!/bin/sh\necho deploy\n")
    os.chmod(str(root / "deploy.sh"), 0o755)
    os.symlink("src/math.py", str(root / "link.py"))
    (tmp_path / "agent.sh").write_text(EVIL_AGENT)

    env = {"REWIND_HOME": str(tmp_path / "home"), "NO_COLOR": "1"}
    assert run_rewind(["init", "--no-hooks"], root, env).returncode == 0
    before = fingerprint(root)
    res = run_rewind(["run", "--name", "evil", "--", "bash", str(tmp_path / "agent.sh")], root, env)
    assert res.returncode == 0, res.stderr

    damaged = fingerprint(root)
    assert "data/.env" not in damaged and "data/local.db" not in damaged
    assert ".git/index" not in damaged
    assert damaged["src/math.py"] != before["src/math.py"]

    res = run_rewind(["undo", "--session", "-y"], root, env)
    assert res.returncode == 0, res.stderr
    assert fingerprint(root) == before

    # and git is healthy again, with the uncommitted work intact
    status = subprocess.run(["git", "status", "--porcelain"], cwd=str(root), capture_output=True, text=True)
    assert status.returncode == 0
    assert " M src/math.py" in status.stdout

    # the undo is itself undoable: the wrecked state is still a snapshot
    assert "Previous state saved as #2" in res.stdout
    assert run_rewind(["restore", "2", "-y"], root, env).returncode == 0
    assert fingerprint(root) == damaged
