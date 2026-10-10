import json
import os
import time

import pytest

from conftest import write
from rewind_cli import notify, registry
from rewind_cli.store import Repo, RewindError, rewind_home
from rewind_cli.watch import daemon_pid


def make_folder(tmp_path, name="proj", files=None):
    root = tmp_path / name
    root.mkdir()
    for rel, text in (files or {"a.txt": "a\n"}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def test_register_is_idempotent_and_persisted(tmp_path):
    root = make_folder(tmp_path)
    e1 = registry.register(root)
    e2 = registry.register(str(root) + "/")
    assert e1["id"] == e2["id"] == registry.folder_id(root.resolve())
    assert [f["path"] for f in registry.load()] == [str(root.resolve())]
    data = json.loads((rewind_home() / "registry.json").read_text())
    assert data["folders"][0]["name"] == "proj" and data["folders"][0]["protected"] is False
    registry.update(e1["id"], protected=True)
    assert registry.get(e1["id"])["protected"] is True
    registry.unregister(e1["id"])
    assert registry.load() == []
    with pytest.raises(RewindError):
        registry.get(e1["id"])


def test_check_folder_rules(tmp_path, fake_home):
    root = make_folder(tmp_path)
    assert registry.check_folder(str(root))["ok"]
    assert "Choose a folder" in registry.check_folder("")["error"]
    assert "full path" in registry.check_folder("relative/path")["error"]
    assert "doesn't exist" in registry.check_folder(str(tmp_path / "nope"))["error"]
    assert "not a folder" in registry.check_folder(str(root / "a.txt"))["error"]
    assert "system folder" in registry.check_folder("/")["error"]
    assert "home folder" in registry.check_folder(str(fake_home))["error"]
    entry = registry.register(root)
    res = registry.check_folder(str(root))
    assert not res["ok"] and res["already"] == entry["id"]
    inner = root / "sub"
    inner.mkdir()
    assert "inside proj" in registry.check_folder(str(inner))["error"]
    res = registry.check_folder(str(tmp_path))
    assert "contains proj" in res["error"] and res["container"]


def test_setup_folder_creates_store_and_ignore(tmp_path):
    root = make_folder(tmp_path)
    repo = registry.setup_folder(root)
    assert (root / ".rewind" / "project.json").exists()
    assert (root / ".rewindignore").exists()
    assert registry.setup_folder(root).store == repo.store  # reuses
    g = make_folder(tmp_path, "g")
    repo_g = registry.setup_folder(g, global_store=True)
    assert repo_g.is_global and not (g / ".rewind").exists()


def test_status_of_folder(tmp_path):
    root = make_folder(tmp_path)
    repo = registry.setup_folder(root)
    repo.capture(message="first", trigger="init")
    entry = registry.register(root)
    st = registry.status(entry)
    assert st["state"] == "paused" and st["versions"] == 1 and st["bytes"] > 0 and st["last_saved"]
    registry.update(entry["id"], protected=True)
    assert registry.status(registry.get(entry["id"]))["state"] == "starting"  # wanted, but no watcher yet
    gone = registry.register(make_folder(tmp_path, "gone"))
    import shutil

    shutil.rmtree(str(tmp_path / "gone"))
    assert registry.status(gone)["state"] == "missing"


def test_protect_starts_and_pause_stops_the_watcher(tmp_path, stop_watchers):
    root = make_folder(tmp_path)
    repo = registry.setup_folder(root)
    repo.capture(trigger="init")
    entry = registry.register(root)
    registry.set_protected(entry["id"], True)
    assert daemon_pid(repo)
    assert registry.status(registry.get(entry["id"]))["state"] == "protected"
    # the watcher really snapshots changes
    # (keep touching the file: the first write can land before the watcher's first scan)
    deadline = time.time() + 20
    n = 0
    while time.time() < deadline and "b.txt" not in repo.snapshot_files(repo.snapshot_ids()[-1]):
        if n % 15 == 0:
            write(repo, "b.txt", "new file %d" % n)
        n += 1
        time.sleep(0.2)
    assert "b.txt" in repo.snapshot_files(repo.snapshot_ids()[-1])
    registry.set_protected(entry["id"], False)
    assert daemon_pid(repo) is None
    kinds = [e["kind"] for e in registry.activity()]
    assert kinds[:2] == ["paused", "protected"]


def test_ensure_watchers_restarts_wanted_ones(tmp_path, stop_watchers):
    root = make_folder(tmp_path)
    repo = registry.setup_folder(root)
    repo.capture(trigger="init")
    entry = registry.register(root, protected=True)
    other = registry.register(registry.setup_folder(make_folder(tmp_path, "other")).root)
    started = registry.ensure_watchers()
    assert started == [entry["id"]]
    assert registry.ensure_watchers() == []  # already running
    assert daemon_pid(registry.repo_for(other)) is None


def test_cli_init_registers_and_watch_updates_intent(project, cli, stop_watchers, tmp_path, monkeypatch):
    fresh = make_folder(tmp_path, "fresh")
    monkeypatch.chdir(fresh)
    code, out, _ = cli("init", "--no-hooks")
    assert code == 0
    entry = registry.find_by_path(fresh)
    assert entry and entry["protected"] is False and entry["source"] == "cli"
    cli("watch", "--daemon")
    assert registry.get(entry["id"])["protected"] is True
    cli("watch", "stop")
    assert registry.get(entry["id"])["protected"] is False


def test_settings_roundtrip_and_validation():
    assert registry.settings() == {"notifications": True, "onboarded": False}
    registry.save_settings(onboarded=True, notifications=False)
    assert registry.settings() == {"notifications": False, "onboarded": True}
    with pytest.raises(RewindError):
        registry.save_settings(bogus=1)
    with pytest.raises(RewindError):
        registry.save_settings(notifications="yes")


def test_activity_feed_newest_first_filter_and_trim(tmp_path, monkeypatch):
    root = make_folder(tmp_path)
    entry = registry.register(root)
    registry.log("restored", "one", folder=entry)
    registry.log("blocked", "two", folder=str(root), detail="rm -rf /")
    registry.log("settings", "three")
    ev = registry.activity()
    assert [e["title"] for e in ev] == ["three", "two", "one"]
    assert ev[1]["folder"] == entry["id"] and ev[1]["folder_name"] == "proj" and ev[1]["detail"] == "rm -rf /"
    assert [e["title"] for e in registry.activity(folder=entry["id"])] == ["two", "one"]
    assert len(registry.activity(limit=1)) == 1
    monkeypatch.setattr(registry, "ACTIVITY_MAX_BYTES", 2000)
    monkeypatch.setattr(registry, "ACTIVITY_KEEP", 5)
    for i in range(40):
        registry.log("settings", "event %d" % i)
    lines = (rewind_home() / "activity.jsonl").read_text().splitlines()
    assert len(lines) <= 30
    assert registry.activity(limit=1)[0]["title"] == "event 39"


def test_notify_commands_per_platform():
    found = lambda name: "/usr/bin/" + name
    mac = notify.command_for('Rewind "blocked"', "rm -rf ~", platform="darwin", which=found)
    assert mac[:2] == ["osascript", "-e"] and '\\"blocked\\"' in mac[2] and "display notification" in mac[2]
    linux = notify.command_for("Title", "Body", platform="linux", which=found)
    assert linux[0] == "/usr/bin/notify-send" and linux[-2:] == ["Title", "Body"]
    assert notify.command_for("t", "m", platform="linux", which=lambda n: None) is None
    assert notify.command_for("t", "m", platform="win32", which=found) is None


def test_notify_respects_opt_out(monkeypatch):
    calls = []
    monkeypatch.delenv("REWIND_NO_NOTIFY", raising=False)
    monkeypatch.setattr(notify, "command_for", lambda t, m: ["true"])
    monkeypatch.setattr(notify.subprocess, "Popen", lambda argv, **kw: calls.append(argv))
    assert notify.desktop("t", "m") is True and calls == [["true"]]
    registry.save_settings(notifications=False)
    assert notify.desktop("t", "m") is False and len(calls) == 1
    monkeypatch.setenv("REWIND_NO_NOTIFY", "1")
    registry.save_settings(notifications=True)
    assert notify.desktop("t", "m") is False


def test_seatbelt_block_lands_in_activity_feed(project, monkeypatch):
    from rewind_cli import hooks

    project.capture()
    shown = []
    monkeypatch.setattr(notify, "desktop", lambda title, msg: shown.append((title, msg)))
    payload = {"session_id": "abc", "cwd": str(project.root), "tool_name": "Bash",
               "tool_input": {"command": "git reset --hard HEAD"}}
    code, _out, err = hooks.run_hook("claude", "PreToolUse", json.dumps(payload))
    assert code == 2 and "seatbelt blocked" in err
    ev = registry.activity()[0]
    assert ev["kind"] == "blocked" and ev["detail"] == "git reset --hard HEAD" and "Claude Code" in ev["title"]
    assert ev["folder"] == registry.folder_id(project.root.resolve())
    assert shown and "git reset --hard" in shown[0][1]
