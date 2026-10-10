"""Retention policy and garbage collection of unreferenced objects."""

import os
import time

from .store import read_json


def select_keep(snapshots, now, keep_all_hours=24, hourly_days=7, daily_days=30, protected=()):
    """Decide which snapshot ids survive.

    * everything younger than ``keep_all_hours`` is kept;
    * then the newest snapshot of each hour, up to ``hourly_days``;
    * then the newest snapshot of each day, up to ``daily_days``;
    * older snapshots are dropped. The latest snapshot is always kept, as are
      ``protected`` ids (session start points) that are still inside the daily window.
    """
    keep = set()
    if not snapshots:
        return keep
    ordered = sorted(snapshots, key=lambda m: m["time"], reverse=True)
    keep.add(ordered[0]["id"])
    seen_hours, seen_days = set(), set()
    all_cut = keep_all_hours * 3600
    hourly_cut = hourly_days * 86400
    daily_cut = daily_days * 86400
    protected = set(protected)
    for meta in ordered:
        age = now - meta["time"]
        if age < all_cut:
            keep.add(meta["id"])
        elif age < hourly_cut:
            bucket = int(meta["time"] // 3600)
            if bucket not in seen_hours:
                seen_hours.add(bucket)
                keep.add(meta["id"])
        elif age < daily_cut:
            bucket = time.strftime("%Y-%m-%d", time.localtime(meta["time"]))
            if bucket not in seen_days:
                seen_days.add(bucket)
                keep.add(meta["id"])
        if meta["id"] in protected and age < daily_cut:
            keep.add(meta["id"])
    return keep


def run_gc(repo, dry_run=False, now=None):
    now = time.time() if now is None else now
    cfg = repo.config
    with repo.lock:
        snaps = repo.list_snapshots()
        sessions = repo.list_sessions()
        protected = [s["start_snapshot"] for s in sessions if s.get("start_snapshot") is not None]
        keep = select_keep(
            snaps,
            now,
            float(cfg.get("gc.keep_all_hours", 24)),
            float(cfg.get("gc.hourly_days", 7)),
            float(cfg.get("gc.daily_days", 30)),
            protected,
        )
        drop = [m for m in snaps if m["id"] not in keep]
        kept = [m for m in snaps if m["id"] in keep]
        # live objects: manifests of kept snapshots, their files, and the stat index
        live = set()
        for meta in kept:
            live.add(meta["manifest"])
            for entry in repo.load_manifest(meta["manifest"]).values():
                if "h" in entry:
                    live.add(entry["h"])
        index = read_json(repo.store / "index.json", {}) or {}
        for row in index.values():
            if len(row) > 4:
                live.add(row[4])
        dead_objects, dead_bytes = [], 0
        for sha, path in repo.iter_objects():
            if sha not in live:
                try:
                    dead_bytes += path.stat().st_size
                except OSError:
                    pass
                dead_objects.append(path)
        if not dry_run:
            for meta in drop:
                repo.delete_snapshot(meta["id"])
            for path in dead_objects:
                try:
                    os.unlink(str(path))
                except OSError:
                    pass
            # re-link parents and sessions so nothing points at removed snapshots
            dropped = {m["id"] for m in drop}
            if dropped:
                for s in sessions:
                    changed = False
                    if s.get("snapshots"):
                        new = [i for i in s["snapshots"] if i not in dropped]
                        changed = new != s["snapshots"]
                        s["snapshots"] = new
                    if changed:
                        repo.save_session(s)
                st = repo.state()
                if st.get("undo"):
                    st.pop("undo")
                    repo.save_state(st)
        return {
            "snapshots_total": len(snaps),
            "snapshots_deleted": len(drop),
            "deleted_ids": [m["id"] for m in drop],
            "objects_deleted": len(dead_objects),
            "bytes_freed": dead_bytes,
            "dry_run": dry_run,
        }
