import json
import os
import sys
import time

import pytest

from conftest import read, run_rewind, write
from rewind_cli import term


def test_main_help(cli):
    code, out, _ = cli("--help")
    assert code == 0
    assert "undo" in out and "rewind run -- claude" in out


def test_subcommand_help_has_examples(cli):
    code, out, _ = cli("undo", "--help")
    assert code == 0 and "examples:" in out and "rewind undo --session" in out
    code, out, _ = cli("help", "run")
    assert code == 0 and "rewind run -- claude" in out


def test_version(cli):
    code, out, _ = cli("--version")
    assert code == 0 and out.startswith("rewind ")


def test_unknown_command(cli):
    code, out, err = cli("frobnicate")
    assert code == 2 and "invalid choice" in err


def test_not_initialized_is_a_clean_error(cli, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out, err = cli("log")
    assert code == 1
    assert "rewind init" in err
    assert "Traceback" not in err


def test_init_snap_log_status(cli, tmp_path, monkeypatch):
    root = tmp_path / "p"
    root.mkdir()
    (root / "a.txt").write_text("a\n")
    monkeypatch.chdir(root)
    code, out, _ = cli("init", "--no-hooks")
    assert code == 0 and "Initialized" in out
    assert (root / ".rewindignore").exists()
    assert "plaintext" in out and "not encrypted" in out
    assert not (root / ".rewind").exists()
    code, out, err = cli("init", "--local", "--global", "--no-hooks")
    assert code == 2
    local = tmp_path / "local"
    local.mkdir()
    monkeypatch.chdir(local)
    code, out, _ = cli("init", "--local", "--no-hooks")
    assert code == 0 and (local / ".rewind" / ".gitignore").read_text().strip().endswith("*")
    monkeypatch.chdir(root)
    code, out, _ = cli("snap")
    assert "Nothing changed" in out
    (root / "a.txt").write_text("b\n")
    code, out, _ = cli("snap", "-m", "second")
    assert "#2" in out and "~1" in out
    code, out, _ = cli("log", "--files")
    assert '"second"' in out and "M a.txt" in out
    code, out, _ = cli("status")
    assert "2 snapshots, 0 sessions" in out
    assert "Files match the last snapshot." in out
    code, out, _ = cli("init")
    assert "Already initialized" in out


def test_no_color_and_forced_color(project, cli, monkeypatch):
    project.capture()
    code, out, _ = cli("log")
    assert "\033[" not in out
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("REWIND_COLOR", "always")
    term.set_color(None)
    code, out, _ = cli("log")
    assert "\033[" in out


def test_status_plural_and_coloured_summary(project, cli, monkeypatch):
    project.capture()
    code, out, _ = cli("status")
    assert code == 0
    assert "1 snapshot, 0 sessions" in out
    assert "Files match the last snapshot." in out
    assert "\033[" not in out
    write(project, "src/app.py", "print('changed')\n")
    (project.root / "new.txt").write_text("x\n")
    code, out, _ = cli("status")
    assert "2 files differ." in out
    assert "\033[" not in out
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("REWIND_COLOR", "always")
    term.set_color(None)
    code, out, _ = cli("status")
    assert "\033[" in out and "2 files differ." in out
    term.set_color(None)


def test_show_and_diff(project, cli):
    project.capture()
    write(project, "src/app.py", "print('changed')\n")
    project.capture()
    code, out, _ = cli("show", "2", "-p")
    assert "M src/app.py" in out
    assert "-print('hello')" in out and "+print('changed')" in out
    write(project, "src/app.py", "print('working')\n")
    code, out, _ = cli("diff")
    assert "+print('working')" in out
    code, out, _ = cli("diff", "1", "2", "src/app.py")
    assert "+print('changed')" in out
    code, out, _ = cli("diff", "1", "2", "README.md")
    assert "No differences" in out
    code, out, _ = cli("diff", "1", "--stat")
    assert "M src/app.py" in out


def test_binary_diff_is_summarized(project, cli):
    write(project, "x.bin", b"\0\1\2")
    project.capture()
    write(project, "x.bin", b"\0\1\3\4")
    code, out, _ = cli("diff")
    assert "Binary files differ" in out


def test_undo_requires_confirmation_when_not_interactive(project, cli, monkeypatch):
    project.capture()
    write(project, "src/app.py", "oops")
    monkeypatch.setattr(sys, "stdin", open(os.devnull))
    code, out, err = cli("undo")
    assert code == 1 and "-y" in err
    assert read(project, "src/app.py") == "oops"
    code, out, _ = cli("undo", "--dry-run")
    assert code == 0 and "dry run" in out
    assert read(project, "src/app.py") == "oops"
    code, out, _ = cli("undo", "-y")
    assert code == 0 and read(project, "src/app.py") == "print('hello')\n"
    code, out, _ = cli("redo", "-y")
    assert code == 0 and read(project, "src/app.py") == "oops"


def test_restore_back_recover_commands(project, cli):
    project.capture()
    os.remove(str(project.root / ".env"))
    code, out, _ = cli("recover", ".env", "-y")
    assert code == 0 and read(project, ".env") == "SECRET=abc\n"
    write(project, "src/app.py", "zzz")
    code, out, _ = cli("restore", "1", "src/app.py", "-y")
    assert code == 0 and read(project, "src/app.py") == "print('hello')\n"
    write(project, "src/app.py", "yyy")
    code, out, _ = cli("back", "0s", "-y")
    assert code == 0 and read(project, "src/app.py") == "print('hello')\n"
    code, out, err = cli("back", "soon")
    assert code == 1 and "could not understand" in err


def test_guard_cli(project, cli):
    code, out, _ = cli("guard", "check", "git reset --hard")
    assert code == 2 and "BLOCK" in out
    code, out, _ = cli("guard", "check", "ls -la")
    assert code == 0
    code, out, _ = cli("guard", "allow", "git reset --hard*")
    assert code == 0
    code, out, _ = cli("guard", "check", "git reset --hard")
    assert code == 0
    code, out, _ = cli("guard", "rules")
    assert "git-clean" in out


def test_config_cli(project, cli):
    code, out, _ = cli("config")
    assert "guard.mode" in out
    code, out, _ = cli("config", "set", "guard.mode", "snapshot")
    assert code == 0
    code, out, _ = cli("config", "get", "guard.mode")
    assert out.strip() == "snapshot"
    code, out, err = cli("config", "set", "guard.mode", "yolo")
    assert code == 1 and "must be one of" in err
    code, out, err = cli("config", "set", "nope", "1")
    assert code == 1 and "unknown config key" in err
    code, out, err = cli("config", "set", "max_file_size_mb", "big")
    assert code == 1 and "number" in err


def test_hook_cli(project, cli):
    code, out, _ = cli("hook", "install", "claude")
    assert code == 0
    code, out, _ = cli("hook", "status")
    assert "installed" in out
    code, out, err = cli("hook", "install", "vim")
    assert code == 1 and "unknown agent" in err


def test_gc_and_doctor(project, cli):
    project.capture()
    code, out, _ = cli("gc", "--dry-run")
    assert "Nothing to collect" in out
    code, out, _ = cli("doctor", "--full")
    assert code == 0 and "No problems found" in out
    # corrupt an object and doctor notices
    sha = project.snapshot_files(1)["README.md"]["h"]
    project.object_path(sha).write_bytes(b"garbage")
    code, out, _ = cli("doctor", "--full")
    assert code == 1 and "corrupt" in out


def test_run_wraps_command_as_session(project, cli):
    project.capture()
    script = "import pathlib; pathlib.Path('made.txt').write_text('x'); pathlib.Path('README.md').unlink()"
    code, out, err = cli("run", "--name", "fake-agent", "--", sys.executable, "-c", script)
    assert code == 0
    assert "session s1" in err
    sess = project.get_session("s1")
    assert sess["name"] == "fake-agent" and sess["exit_code"] == 0 and sess["end_snapshot"]
    code, out, _ = cli("undo", "--session", "-y")
    assert code == 0 and "redo" in out
    assert not (project.root / "made.txt").exists()
    assert read(project, "README.md") == "# demo\n"
    code, out, _ = cli("redo", "-y")
    assert code == 0
    assert (project.root / "made.txt").read_text() == "x"
    assert not (project.root / "README.md").exists()


def test_oops_previews_then_undoes_the_session(project, cli):
    project.capture()
    script = "import pathlib; pathlib.Path('made.txt').write_text('x')"
    code, _, _ = cli("run", "--name", "fake-agent", "--", sys.executable, "-c", script)
    assert code == 0 and (project.root / "made.txt").exists()
    code, out, _ = cli("oops", "--dry-run")
    assert code == 0 and "dry run" in out and (project.root / "made.txt").exists()
    code, out, _ = cli("oops", "-y")
    assert code == 0 and "undone" in out.lower() and not (project.root / "made.txt").exists()
    code, out, _ = cli("redo", "-y")
    assert code == 0 and (project.root / "made.txt").read_text() == "x"


def test_run_propagates_exit_code_and_missing_command(project, cli):
    code, _, _ = cli("run", "--", sys.executable, "-c", "raise SystemExit(7)")
    assert code == 7
    code, _, err = cli("run", "--", "definitely-not-a-real-command-xyz")
    assert code == 1 and "command not found" in err
    code, _, err = cli("run")
    assert code == 1


def test_subprocess_entrypoint_and_startup(project):
    t0 = time.time()
    res = run_rewind(["--version"], project.root)
    assert res.returncode == 0 and "rewind" in res.stdout
    assert time.time() - t0 < 5  # generous: CI machines vary; real startup is ~30-60 ms


def test_hook_exec_via_subprocess(project):
    res = run_rewind(["hook", "exec", "claude", "PreToolUse"], project.root, input_text=json.dumps(
        {"session_id": "x", "cwd": str(project.root), "tool_name": "Bash", "tool_input": {"command": "rm -rf ~"}}))
    assert res.returncode == 2
    assert "home directory" in res.stderr


def test_gc_cli_applies_retention_with_confirmation(project, cli, monkeypatch):
    from rewind_cli.store import write_json

    project.capture()
    write(project, "a.txt", "a")
    project.capture()
    meta = project.get_snapshot(1)
    meta["time"] -= 90 * 86400
    write_json(project.snap_path(1), meta)
    monkeypatch.setattr(sys, "stdin", open(os.devnull))
    code, out, err = cli("gc")
    assert code == 1 and "-y" in err and project.snapshot_ids() == [1, 2]
    code, out, _ = cli("gc", "-y")
    assert code == 0 and "Deleted 1 of 2" in out
    assert project.snapshot_ids() == [2]
