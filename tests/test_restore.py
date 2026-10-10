import os
import stat

import pytest

from conftest import read, write
from rewind_cli import restore
from rewind_cli.scan import scan
from rewind_cli.store import RewindError


def test_restore_modifications_deletions_and_new_files(project):
    project.capture()
    write(project, "src/app.py", "broken\n")
    os.remove(str(project.root / "README.md"))
    write(project, "junk/new.txt", "agent garbage")
    res = restore.restore(project, 1)
    assert not res.errors
    assert read(project, "src/app.py") == "print('hello')\n"
    assert read(project, "README.md") == "# demo\n"
    assert not (project.root / "junk").exists(), "new file and its now-empty dir are removed"
    assert res.safety_id == 2
    # the safety snapshot really holds the pre-restore state
    assert project.snapshot_files(res.safety_id)["junk/new.txt"]


def test_restore_is_itself_undoable(project):
    project.capture()
    write(project, "src/app.py", "v2\n")
    project.capture()
    res = restore.restore(project, 1)
    assert read(project, "src/app.py") == "print('hello')\n"
    restore.restore(project, res.safety_id)
    assert read(project, "src/app.py") == "v2\n"


def test_dry_run_changes_nothing(project):
    project.capture()
    write(project, "src/app.py", "changed\n")
    ids_before = project.snapshot_ids()
    res = restore.restore(project, 1, dry_run=True)
    assert res.plan.writes and res.dry_run
    assert read(project, "src/app.py") == "changed\n"
    assert project.snapshot_ids() == ids_before


def test_restore_specific_paths_only(project):
    project.capture()
    write(project, "src/app.py", "changed\n")
    write(project, "README.md", "changed\n")
    write(project, "src/extra.py", "x")
    restore.restore(project, 1, ["src/app.py"])
    assert read(project, "src/app.py") == "print('hello')\n"
    assert read(project, "README.md") == "changed\n"
    assert (project.root / "src/extra.py").exists()


def test_restore_directory_scope_and_glob(project):
    write(project, "docs/a.md", "a")
    write(project, "docs/b.txt", "b")
    project.capture()
    write(project, "docs/a.md", "A!")
    write(project, "docs/b.txt", "B!")
    restore.restore(project, 1, ["docs/*.md"])
    assert read(project, "docs/a.md") == "a"
    assert read(project, "docs/b.txt") == "B!"
    restore.restore(project, 1, ["docs"])
    assert read(project, "docs/b.txt") == "b"


def test_keep_new_files(project):
    project.capture()
    write(project, "new.txt", "keep me")
    restore.restore(project, 1, delete=False)
    assert (project.root / "new.txt").exists()


def test_binary_files_roundtrip(project):
    blob = bytes(range(256)) * 50 + b"\0\0\xff"
    write(project, "img.bin", blob)
    project.capture()
    write(project, "img.bin", b"corrupted")
    restore.restore(project, 1)
    assert (project.root / "img.bin").read_bytes() == blob


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_file_modes_are_preserved(project):
    write(project, "run.sh", "#!/bin/sh\necho hi\n", mode=0o755)
    project.capture()
    os.remove(str(project.root / "run.sh"))
    restore.restore(project, 1)
    assert stat.S_IMODE(os.stat(str(project.root / "run.sh")).st_mode) == 0o755
    # a mode-only change is detected and reverted
    os.chmod(str(project.root / "run.sh"), 0o600)
    assert restore.restore(project, 1).plan.writes
    assert stat.S_IMODE(os.stat(str(project.root / "run.sh")).st_mode) == 0o755


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlinks_are_restored(project):
    os.symlink("src/app.py", str(project.root / "link.py"))
    project.capture()
    os.remove(str(project.root / "link.py"))
    write(project, "link.py", "now a regular file")
    restore.restore(project, 1)
    assert os.path.islink(str(project.root / "link.py"))
    assert os.readlink(str(project.root / "link.py")) == "src/app.py"


def test_file_becomes_directory_and_back(project):
    write(project, "thing", "i am a file")
    project.capture()
    os.remove(str(project.root / "thing"))
    write(project, "thing/inner.txt", "now a dir")
    project.capture()
    restore.restore(project, 1)
    assert read(project, "thing") == "i am a file"
    restore.restore(project, 2)
    assert read(project, "thing/inner.txt") == "now a dir"


def test_renames_are_reverted(project):
    project.capture()
    os.rename(str(project.root / "src"), str(project.root / "lib"))
    restore.restore(project, 1)
    assert read(project, "src/app.py") == "print('hello')\n"
    assert not (project.root / "lib").exists()


def test_ignored_files_are_never_touched(project):
    write(project, "node_modules/dep.js", "dep v1")
    project.capture()
    write(project, "node_modules/dep.js", "dep v2")
    write(project, "node_modules/new.js", "new")
    restore.restore(project, 1)
    assert read(project, "node_modules/dep.js") == "dep v2"
    assert (project.root / "node_modules/new.js").exists()


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_never_writes_through_symlink_leaving_project(project, tmp_path):
    write(project, "data/file.txt", "inside")
    project.capture()
    outside = tmp_path / "victim"
    outside.mkdir()
    import shutil

    shutil.rmtree(str(project.root / "data"))
    os.symlink(str(outside), str(project.root / "data"))
    # restoring only out/file.txt must not follow the symlink outside the project
    res = restore.restore(project, 1, ["data/file.txt"])
    assert res.errors
    assert not (outside / "file.txt").exists()
    # a full restore removes the symlink first and recreates the real directory
    res = restore.restore(project, 1)
    assert not res.errors
    assert not os.path.islink(str(project.root / "data"))
    assert read(project, "data/file.txt") == "inside"


def test_restore_unknown_snapshot(project):
    project.capture()
    with pytest.raises(RewindError):
        restore.restore(project, 42)


def test_scan_after_restore_matches_snapshot(project):
    write(project, "a/b/c.txt", "c")
    project.capture()
    import shutil

    shutil.rmtree(str(project.root / "a"))
    write(project, "src/app.py", "zzz")
    restore.restore(project, 1)
    assert project.put_manifest(scan(project).files) == project.get_snapshot(1)["manifest"]
