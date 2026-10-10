import time

from conftest import write
from rewind_cli.gc import run_gc, select_keep
from rewind_cli.store import write_json

H = 3600
D = 86400


def snaps(ages, now):
    return [{"id": i + 1, "time": now - age} for i, age in enumerate(sorted(ages, reverse=True))]


def test_keep_everything_recent():
    now = 1_700_000_000.0
    s = snaps([60 * i for i in range(1, 50)], now)  # all within 1 hour
    assert select_keep(s, now) == {m["id"] for m in s}


def test_hourly_then_daily_then_drop():
    now = 1_700_000_000.0
    ages = []
    # 4 snapshots inside each of hours 30..33 ago (hourly window)
    for h in range(30, 34):
        ages += [h * H + m * 60 for m in (1, 10, 20, 30)]
    # 3 per day at days 10..12 (daily window)
    for d in range(10, 13):
        ages += [d * D + k * H for k in (1, 2, 3)]
    ages += [40 * D, 41 * D]  # beyond 30 days
    s = snaps(ages, now)
    keep = select_keep(s, now, keep_all_hours=24, hourly_days=7, daily_days=30)
    kept = [m for m in s if m["id"] in keep]
    hourly = [m for m in kept if 24 * H <= now - m["time"] < 7 * D]
    daily = [m for m in kept if 7 * D <= now - m["time"] < 30 * D]
    old = [m for m in kept if now - m["time"] >= 30 * D]
    assert 4 <= len(hourly) <= 5  # one per clock hour bucket
    assert 3 <= len(daily) <= 4  # one per calendar day bucket
    assert old == []


def test_latest_snapshot_always_kept():
    now = 1_700_000_000.0
    s = snaps([100 * D, 200 * D], now)
    assert select_keep(s, now) == {2}


def test_protected_session_starts_kept_within_daily_window():
    now = 1_700_000_000.0
    s = snaps([2 * D + 10, 2 * D + 20, 2 * D + 30], now)
    keep = select_keep(s, now, protected={1})
    assert 1 in keep


def test_run_gc_deletes_old_snapshots_and_orphaned_objects(project):
    project.capture()                       # 1
    write(project, "big.txt", "old content " * 100)
    project.capture()                       # 2 (has big.txt)
    write(project, "big.txt", "new content")
    project.capture()                       # 3
    for sid, age in ((1, 60 * D), (2, 59 * D)):
        meta = project.get_snapshot(sid)
        meta["time"] = time.time() - age
        write_json(project.snap_path(sid), meta)
    old_sha = project.snapshot_files(2)["big.txt"]["h"]
    dry = run_gc(project, dry_run=True)
    assert dry["snapshots_deleted"] == 2 and project.has_object(old_sha)
    res = run_gc(project)
    assert res["deleted_ids"] == [1, 2]
    assert not project.has_object(old_sha)
    assert project.snapshot_ids() == [3]
    # everything still restorable from the remaining snapshot
    for entry in project.snapshot_files(3).values():
        assert project.has_object(entry["h"])


def test_gc_keeps_objects_referenced_by_index(project):
    project.capture()
    write(project, "draft.txt", "unsnapshotted but indexed")
    from rewind_cli.scan import scan

    files = scan(project).files  # writes the object + index, no snapshot
    run_gc(project)
    assert project.has_object(files["draft.txt"]["h"])
