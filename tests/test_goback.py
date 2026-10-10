"""Plain-language previews for 'Undo last change' and 'Go back in time'."""
import time

import pytest

from conftest import write
from rewind_cli import plain, restore
from rewind_cli.app import AppApi
from rewind_cli import registry
from rewind_cli.restore import Plan
from rewind_cli.store import read_json, write_json


def plan_of(creates=(), modifies=(), deletes=()):
    p = Plan(target_id=4)
    p.writes = [(x, {}, "create") for x in creates] + [(x, {}, "modify") for x in modifies]
    p.deletes = list(deletes)
    return p


@pytest.mark.parametrize("args,sentence", [
    (dict(creates=["a"]), "1 file will come back."),
    (dict(creates=["a", "b", "c"], deletes=["d"]), "3 files will come back and 1 file will be removed."),
    (dict(creates=["a"], modifies=["b", "c"], deletes=["d", "e"]),
     "1 file will come back, 2 files will change back and 2 files will be removed."),
    (dict(modifies=[".git/index", ".git/HEAD"]), "Only Git's internal files differ (2). Your own files already match."),
    (dict(), "Your files already match. Nothing would change."),
])
def test_describe_plan_sentences(args, sentence):
    assert plain.describe_plan(plan_of(**args))["sentence"] == sentence


def test_describe_plan_separates_git_internals():
    s = plain.describe_plan(plan_of(creates=["src/a.py", ".git/objects/ab/cd"], modifies=[".gitignore"], deletes=[".git/ORIG_HEAD"]))
    assert s["come_back"] == ["src/a.py"] and s["change_back"] == [".gitignore"] and s["git_internal"] == 2
    assert s["total"] == 4 and s["target_id"] == 4


def test_describe_snapshot_titles():
    sessions = {"s1": {"id": "s1", "agent": "claude", "name": "Claude Code"}}
    base = {"id": 3, "time": 0, "stats": {}, "changes": [["M", "src/a.py"], ["D", "b.txt"], ["M", ".git/index"]], "nchanges": 3}
    hook = plain.describe_snapshot(dict(base, trigger="hook", tool="Edit", session="s1", message="after Edit"), sessions)
    assert hook["title"] == "Claude Code changed files" and hook["summary"] == "1 file edited, 1 file deleted"
    assert hook["files"] == ["src/a.py", "b.txt"]
    blocked = plain.describe_snapshot(dict(base, trigger="guard", message="blocked: git reset --hard discards uncommitted work", session="s1"), sessions)
    assert blocked["kind"] == "blocked" and blocked["title"] == "Blocked a dangerous command from Claude Code"
    assert plain.describe_snapshot(dict(base, trigger="manual", message="pre-deploy"))["title"] == "Checkpoint: pre-deploy"
    assert plain.describe_snapshot(dict(base, trigger="restore", message="undo to #2"))["title"] == "Undid the last change"
    assert plain.describe_snapshot(dict(base, trigger="restore", message="recover a.txt"))["title"] == "Brought back deleted files"
    assert plain.describe_snapshot(dict(base, trigger="init", files=12))["summary"] == "12 files saved"
    git_only = plain.describe_snapshot(dict(base, trigger="watch", changes=[["M", ".git/index"]], nchanges=1))
    assert git_only["summary"] == "Git's internal files changed"


def test_when_phrases():
    now = time.mktime((2026, 10, 10, 15, 0, 0, 0, 0, -1))
    assert plain.when(now - 10, now) == "just now"
    assert plain.when(now - 60, now) == "1 minute ago"
    assert plain.when(now - 25 * 60, now) == "25 minutes ago"
    assert plain.when(now - 3 * 3600, now) == "today at 12:00"
    assert plain.when(now - 20 * 3600, now) == "yesterday at 19:00"
    assert plain.when(now - 5 * 86400, now) == "Oct 5 at 15:00"


def backdate(repo, sid, seconds_ago):
    meta = read_json(repo.snap_path(sid))
    meta["time"] = time.time() - seconds_ago
    write_json(repo.snap_path(sid), meta)


def test_presets_pick_the_right_saved_version(project):
    first, _f, _h = project.capture(message="yesterday's state", trigger="init")
    write(project, "src/app.py", "an hour ago\n")
    second, _f, _h = project.capture()
    write(project, "src/app.py", "now\n")
    third, _f, _h = project.capture()
    backdate(project, first["id"], 30 * 3600)
    backdate(project, second["id"], 70 * 60)
    backdate(project, third["id"], 60)
    entry = registry.register(project.root)
    pts = {p["key"]: p for p in AppApi().goback_points(entry["id"])["presets"]}
    assert pts["5m"]["snapshot_id"] == second["id"]
    assert pts["1h"]["snapshot_id"] == second["id"]
    assert pts["yesterday"]["snapshot_id"] == first["id"]
    lt = time.localtime()
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    # "this morning" = the state before today's first change: the newest save before midnight
    expected = restore.snapshot_before(project, midnight) or next(m for m in project.list_snapshots() if m["time"] >= midnight)
    assert pts["morning"]["snapshot_id"] == expected["id"]
    prev = AppApi().preview(entry["id"], "back", first["id"])
    assert prev["summary"]["sentence"] == "1 file will change back." and prev["target_id"] == first["id"]


def test_morning_falls_back_to_first_save_today(project, monkeypatch):
    project.capture(trigger="init")
    entry = registry.register(project.root)
    # everything was saved today, after midnight: "this morning" means the first save today
    pts = {p["key"]: p for p in AppApi().goback_points(entry["id"])["presets"]}
    assert pts["morning"]["snapshot_id"] == project.snapshot_ids()[0]


def test_session_preview_and_apply(project):
    project.capture(trigger="init")
    sess = project.new_session("Claude Code", agent="claude", external="claude:x")
    meta, _f, head = project.capture(session=sess["id"])
    sess = project.get_session(sess["id"])
    sess["start_snapshot"] = head
    project.save_session(sess)
    write(project, "src/app.py", "agent edit\n")
    (project.root / "README.md").unlink()
    project.capture(session=sess["id"], trigger="hook")
    entry = registry.register(project.root)
    api = AppApi()
    p = api.preview(entry["id"], "session", sess["id"])
    assert p["title"] == "Undo the Claude Code session"
    assert p["summary"]["come_back"] == ["README.md"] and p["summary"]["change_back"] == ["src/app.py"]
    res = api.apply(entry["id"], "session", sess["id"], expect=p["target_id"])
    assert res["changed"] == 2 and (project.root / "README.md").read_text() == "# demo\n"


def test_deleted_files_order_ignore_and_git(project):
    write(project, "node_modules/x.js", "ignored")
    write(project, ".git/HEAD", "ref: refs/heads/main\n")
    write(project, "old.txt", "old")
    project.capture()
    (project.root / "old.txt").unlink()
    project.capture()
    (project.root / ".env").unlink()
    (project.root / ".git" / "HEAD").unlink()
    items = restore.deleted_files(project)
    assert [d["path"] for d in items] == [".env", "old.txt"]
    assert items[0]["deleted_at"] is None and items[1]["deleted_in"] == project.snapshot_ids()[-1]
    with_git = restore.deleted_files(project, include_git=True)
    assert ".git/HEAD" in [d["path"] for d in with_git]


def test_recover_paths_is_literal(project):
    write(project, "app/[id].tsx", "page")
    write(project, "app/i.tsx", "other")
    project.capture()
    (project.root / "app" / "[id].tsx").unlink()
    (project.root / "app" / "i.tsx").unlink()
    res = restore.recover_paths(project, ["app/[id].tsx"])
    assert [w[0] for w in res.plan.writes] == ["app/[id].tsx"]
    assert (project.root / "app" / "[id].tsx").exists() and not (project.root / "app" / "i.tsx").exists()
