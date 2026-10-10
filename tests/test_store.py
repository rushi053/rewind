import os
import zlib

import pytest

from conftest import write
from rewind_cli.scan import scan
from rewind_cli.store import Repo, RewindError, NotInitialized


def test_objects_are_content_addressed_and_compressed(project):
    sha = project.put_bytes(b"hello world" * 100)
    raw = project.object_path(sha).read_bytes()
    assert zlib.decompress(raw) == b"hello world" * 100
    assert len(raw) < 1100
    assert project.put_bytes(b"hello world" * 100) == sha  # idempotent
    assert project.get_bytes(sha) == b"hello world" * 100


def test_scan_includes_untracked_env_and_git_dir(project):
    write(project, ".git/HEAD", "ref: refs/heads/main\n")
    files = scan(project).files
    assert ".env" in files
    assert ".git/HEAD" in files
    assert "src/app.py" in files
    assert not any(p.startswith(".rewind/") for p in files)


def test_include_git_false(project):
    write(project, ".git/HEAD", "ref\n")
    project.config.set("include_git", "false")
    assert ".git/HEAD" not in scan(project).files


def test_default_ignores_are_used_without_rewindignore(project):
    write(project, "node_modules/x/index.js", "x")
    write(project, "pkg/__pycache__/a.pyc", "x")
    files = scan(project).files
    assert "node_modules/x/index.js" not in files
    assert "pkg/__pycache__/a.pyc" not in files


def test_rewindignore_file_is_respected(project):
    write(project, ".rewindignore", "*.log\n")
    write(project, "debug.log", "x")
    write(project, "node_modules/x.js", "x")  # custom file replaces defaults
    files = scan(project).files
    assert "debug.log" not in files
    assert "node_modules/x.js" in files


def test_large_files_are_skipped(project):
    project.config.set("max_file_size_mb", "0.001")
    write(project, "big.bin", b"x" * 5000)
    res = scan(project)
    assert "big.bin" not in res.files
    assert ("big.bin", 5000) in res.skipped


def test_incremental_scan_uses_index(project, monkeypatch):
    first = scan(project)
    assert first.hashed >= 3
    # make every file look old so the racy-timestamp guard does not force re-hashing
    for rel in first.files:
        p = project.root / rel
        os.utime(str(p), (1_600_000_000, 1_600_000_000))
    scan(project)  # rebuild index with old mtimes
    again = scan(project)
    assert again.hashed == 0
    write(project, "src/app.py", "print('changed')\n")
    third = scan(project)
    assert third.hashed == 1


def test_snapshot_dedupe_and_noop(project):
    meta, files, head = project.capture(message="one")
    assert meta["id"] == 1 and meta["stats"]["added"] == 3
    again, _, head2 = project.capture(message="two")
    assert again is None and head2 == 1
    count_before = sum(1 for _ in project.iter_objects())
    write(project, "copy.py", "print('hello')\n")  # same content as src/app.py
    meta2, _, _ = project.capture()
    assert meta2["id"] == 2
    assert meta2["stats"] == {"added": 1, "modified": 0, "deleted": 0, "renamed": 0}
    # one new manifest object, but no new content object
    assert sum(1 for _ in project.iter_objects()) == count_before + 1


def test_force_snapshot(project):
    project.capture()
    meta, _, _ = project.capture(force=True, message="forced")
    assert meta and meta["id"] == 2


def test_rename_detection(project):
    project.capture()
    os.rename(str(project.root / "README.md"), str(project.root / "README.txt"))
    meta, _, _ = project.capture()
    assert meta["stats"]["renamed"] == 1
    assert ["R", "README.txt", "README.md"] in meta["changes"]


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlinks_are_recorded_not_followed(project, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("no")
    os.symlink(str(outside), str(project.root / "linkdir"))
    os.symlink("src/app.py", str(project.root / "link.py"))
    files = scan(project).files
    assert files["link.py"] == {"l": "src/app.py"}
    assert files["linkdir"] == {"l": str(outside)}
    assert not any(p.startswith("linkdir/") for p in files)


def test_resolve_refs(project):
    for i in range(3):
        write(project, "f%d" % i, str(i))
        project.capture()
    assert project.resolve("HEAD") == 3
    assert project.resolve("#2") == 2
    assert project.resolve("latest~2") == 1
    with pytest.raises(RewindError):
        project.resolve("99")
    with pytest.raises(RewindError):
        project.resolve("HEAD~9")
    with pytest.raises(RewindError):
        project.resolve("banana")


def test_find_walks_up_and_errors_outside(project, tmp_path):
    assert Repo.find(project.root / "src").root == project.root
    other = tmp_path / "elsewhere"
    other.mkdir()
    with pytest.raises(NotInitialized):
        Repo.find(other)


def test_global_store(tmp_path, monkeypatch):
    root = tmp_path / "g"
    root.mkdir()
    (root / "a.txt").write_text("a")
    repo = Repo.init(root, global_store=True)
    assert not (root / ".rewind").exists()
    assert str(repo.store).startswith(os.environ["REWIND_HOME"])
    assert repo.is_global
    repo.capture()
    found = Repo.find(root)
    assert found.store == repo.store
    assert "a.txt" in found.snapshot_files(1)


def test_lock_is_reentrant_and_released(project):
    with project.lock:
        with project.lock:
            assert (project.store / "lock").exists()
    assert not (project.store / "lock").exists()


def test_stale_lock_is_broken(project):
    (project.store / "lock").write_text("999999999")
    with project.lock:
        pass
