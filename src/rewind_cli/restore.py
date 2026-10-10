"""Restoring snapshots: restore, undo/redo, session undo, back-in-time, recover.

Every operation that touches the working tree first records a *safety
snapshot* of the current state, so any restore can itself be undone.
"""

import fnmatch
import os
import random
import time
from pathlib import Path

from .diff import same_entry
from .scan import scan, update_index
from .store import RewindError


class Plan:
    def __init__(self, target_id=None, label=""):
        self.target_id = target_id
        self.label = label
        self.writes = []  # (path, entry, kind) kind: "modify" | "create"
        self.deletes = []  # paths

    def __bool__(self):
        return bool(self.writes or self.deletes)

    def counts(self):
        return {
            "modify": sum(1 for w in self.writes if w[2] == "modify"),
            "create": sum(1 for w in self.writes if w[2] == "create"),
            "delete": len(self.deletes),
        }


class Result:
    def __init__(self, plan, safety_id=None, post_id=None, errors=None, dry_run=False):
        self.plan = plan
        self.safety_id = safety_id
        self.post_id = post_id
        self.errors = errors or []
        self.dry_run = dry_run


# --------------------------------------------------------------------- scope
def make_scope(patterns):
    """Return a predicate over project-relative paths, or None for 'everything'."""
    pats = [p.strip("/") for p in (patterns or []) if p is not None]
    if not pats or "" in pats:
        return None

    def match(path):
        parts = path.split("/")
        prefixes = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
        for p in pats:
            if path == p or path.startswith(p + "/"):
                return True
            if any(ch in p for ch in "*?["):
                for pre in prefixes:
                    name = pre.rsplit("/", 1)[-1]
                    if fnmatch.fnmatchcase(pre, p) or ("/" not in p and fnmatch.fnmatchcase(name, p)):
                        return True
        return False

    return match


def compute_plan(target, current, scope=None, delete=True, target_id=None, label=""):
    plan = Plan(target_id, label)
    for path in sorted(target):
        if scope and not scope(path):
            continue
        cur = current.get(path)
        if not same_entry(cur, target[path]):
            plan.writes.append((path, target[path], "modify" if cur is not None else "create"))
    if delete:
        for path in sorted(current):
            if path not in target and (scope is None or scope(path)):
                plan.deletes.append(path)
    return plan


# --------------------------------------------------------------------- apply
def _abs(repo, rel):
    return Path(str(repo.root), *rel.split("/"))


def _inside_root(repo, path):
    root = os.path.realpath(str(repo.root))
    real = os.path.realpath(str(path))
    return real == root or real.startswith(root.rstrip(os.sep) + os.sep)


def _prune_empty_dirs(repo, start):
    root = Path(str(repo.root))
    d = start
    while d != root and root in d.parents:
        try:
            d.rmdir()
        except OSError:
            return
        d = d.parent


def apply_plan(repo, plan):
    errors = []
    written = []
    # deletions first, deepest paths first, so file/dir type changes work
    for rel in sorted(plan.deletes, key=lambda p: (-p.count("/"), p)):
        p = _abs(repo, rel)
        try:
            if p.is_symlink() or p.is_file():
                p.unlink()
            elif p.is_dir():
                p.rmdir()
        except FileNotFoundError:
            pass
        except OSError as exc:
            errors.append((rel, "could not delete: %s" % exc))
            continue
        _prune_empty_dirs(repo, p.parent)
    for rel, entry, _kind in plan.writes:
        p = _abs(repo, rel)
        try:
            parent = p.parent
            if parent.exists() and not parent.is_dir():
                raise OSError("a file is in the way at %s" % parent)
            parent.mkdir(parents=True, exist_ok=True)
            if not _inside_root(repo, parent):
                raise OSError("refusing to write through a symlink that leaves the project")
            if p.is_dir() and not p.is_symlink():
                try:
                    p.rmdir()
                except OSError:
                    raise OSError("a non-empty directory is in the way")
            if "l" in entry:
                if p.is_symlink() or p.exists():
                    p.unlink()
                os.symlink(entry["l"], str(p))
            else:
                data = repo.get_bytes(entry["h"])
                tmp = p.with_name(".%s.rewind-%d" % (p.name, random.randint(0, 1 << 30)))
                with open(str(tmp), "wb") as fh:
                    fh.write(data)
                os.chmod(str(tmp), entry.get("m", 0o644))
                os.replace(str(tmp), str(p))
                written.append((rel, entry["h"]))
        except (OSError, NotImplementedError) as exc:
            errors.append((rel, str(exc)))
    if written:
        update_index(repo, written)
    return errors


def execute(repo, compute, reason, dry_run=False, session=None):
    """Scan, compute a plan, take a safety snapshot, apply, record the result."""
    with repo.lock:
        current = scan(repo).files
        plan = compute(current)
        if dry_run or not plan:
            return Result(plan, dry_run=dry_run)
        reason = reason() if callable(reason) else reason
        meta, _files, head_id = repo.capture(
            message="before %s" % reason, trigger="safety", files=current, session=session
        )
        safety_id = meta["id"] if meta else head_id
        errors = apply_plan(repo, plan)
        post, _f, post_head = repo.capture(message=reason, trigger="restore", session=session)
        return Result(plan, safety_id, post["id"] if post else post_head, errors)


# ---------------------------------------------------------------- operations
def restore(repo, ref, paths=None, dry_run=False, delete=True):
    sid = repo.resolve(ref)
    target = repo.snapshot_files(sid)
    scope = make_scope([repo.rel(p) for p in paths] if paths else None)
    what = ", ".join(paths) if paths else "project"
    res = execute(
        repo,
        lambda cur: compute_plan(target, cur, scope, delete=delete, target_id=sid),
        "restore %s to #%d" % (what, sid),
        dry_run,
    )
    if not dry_run and res.plan:
        _clear_undo(repo)
    return res


def _clear_undo(repo):
    st = repo.state()
    if st.pop("undo", None) is not None:
        repo.save_state(st)


def find_undo_target(repo, current_files):
    manifest = repo.put_manifest(current_files)
    ids = repo.snapshot_ids()
    undo = repo.state().get("undo")
    if undo and undo.get("after") == manifest:
        ids = [i for i in ids if i < undo["cursor"]]
    for sid in reversed(ids):
        if repo.get_snapshot(sid)["manifest"] != manifest:
            return sid
    return None


def undo(repo, dry_run=False, paths=None):
    holder = {}
    scope = make_scope([repo.rel(p) for p in paths] if paths else None)

    def compute(current):
        sid = find_undo_target(repo, current)
        if sid is None:
            raise RewindError("nothing to undo: no earlier snapshot differs from the current files")
        holder["sid"] = sid
        holder["manifest"] = repo.put_manifest(current)
        return compute_plan(repo.snapshot_files(sid), current, scope, target_id=sid)

    with repo.lock:
        prev = repo.state().get("undo")
        res = execute(repo, compute, lambda: "undo to #%d" % holder["sid"], dry_run)
        if res.plan and not dry_run:
            sid = holder["sid"]
            continuing = prev and prev.get("after") == holder["manifest"]
            stack = list(prev.get("stack", [])) if continuing else []
            stack.append([res.safety_id, prev.get("cursor") if continuing else None])
            post_manifest = repo.get_snapshot(res.post_id)["manifest"]
            st = repo.state()
            st["undo"] = {"cursor": sid, "after": post_manifest, "stack": stack}
            repo.save_state(st)
        return res


def redo(repo, dry_run=False):
    with repo.lock:
        current = scan(repo).files
        manifest = repo.put_manifest(current)
        st = repo.state()
        u = st.get("undo")
        if not u or u.get("after") != manifest or not u.get("stack"):
            raise RewindError("nothing to redo (redo only works right after `rewind undo`, before other changes)")
        safety_id, prev_cursor = u["stack"][-1]
        target = repo.snapshot_files(safety_id)
        res = execute(repo, lambda cur: compute_plan(target, cur, target_id=safety_id), "redo to #%d" % safety_id, dry_run)
        if not dry_run and res.plan:
            stack = u["stack"][:-1]
            st = repo.state()
            if prev_cursor is None:
                st.pop("undo", None)
            else:
                st["undo"] = {"cursor": prev_cursor, "after": repo.get_snapshot(res.post_id)["manifest"], "stack": stack}
            repo.save_state(st)
        return res


def undo_session(repo, session_id=None, dry_run=False):
    sess = repo.get_session(session_id) if session_id else repo.latest_session()
    if not sess:
        raise RewindError("no agent sessions recorded yet (use `rewind run -- <agent>` or install hooks)")
    start = sess.get("start_snapshot")
    if start is None:
        raise RewindError("session %s has no starting snapshot" % sess["id"])
    target = repo.snapshot_files(start)
    res = execute(
        repo,
        lambda cur: compute_plan(target, cur, target_id=start, label=sess["id"]),
        "undo session %s (back to #%d)" % (sess["id"], start),
        dry_run,
    )
    if not dry_run and res.plan:
        _clear_undo(repo)
    return sess, res


def snapshot_before(repo, ts):
    best = None
    for meta in repo.list_snapshots():
        if meta["time"] <= ts:
            best = meta
        else:
            break
    return best


def back(repo, ts, paths=None, dry_run=False):
    meta = snapshot_before(repo, ts)
    if meta is None:
        snaps = repo.snapshot_ids()
        if not snaps:
            raise RewindError("no snapshots yet")
        first = repo.get_snapshot(snaps[0])
        raise RewindError("no snapshot that old; the oldest is #%d from %s" % (
            first["id"], time.strftime("%Y-%m-%d %H:%M", time.localtime(first["time"]))))
    return meta, restore(repo, meta["id"], paths, dry_run)


def recover(repo, patterns, from_ref=None, dry_run=False):
    rels = [p if any(ch in p for ch in "*?[") and not os.path.isabs(p) else repo.rel(p) for p in patterns]
    scope = make_scope(rels)
    if scope is None:
        raise RewindError("give a path or glob to recover")
    holder = {}

    def compute(current):
        chosen = {}
        ids = [repo.resolve(from_ref)] if from_ref else list(reversed(repo.snapshot_ids()))
        seen_manifests = set()
        for sid in ids:
            meta = repo.get_snapshot(sid)
            if meta["manifest"] in seen_manifests:
                continue
            seen_manifests.add(meta["manifest"])
            for path, entry in repo.load_manifest(meta["manifest"]).items():
                if path not in current and path not in chosen and scope(path):
                    chosen[path] = (entry, sid)
        plan = Plan(label="recover")
        for path in sorted(chosen):
            plan.writes.append((path, chosen[path][0], "create"))
        holder["sources"] = {p: s for p, (e, s) in chosen.items()}
        return plan

    res = execute(repo, compute, "recover %s" % " ".join(patterns), dry_run)
    res.sources = holder.get("sources", {})
    return res


def recover_paths(repo, paths, dry_run=False):
    """Bring back exactly these project-relative paths (no glob expansion: `[id].tsx` is a file name).

    Each path comes from the newest snapshot that has it. Paths that exist now are left alone.
    """
    wanted = {p.strip("/") for p in paths if p and p.strip("/")}
    if not wanted:
        raise RewindError("choose at least one file to bring back")
    holder = {}

    def compute(current):
        chosen = {}
        seen = set()
        for sid in reversed(repo.snapshot_ids()):
            meta = repo.get_snapshot(sid)
            if meta["manifest"] in seen:
                continue
            seen.add(meta["manifest"])
            files = repo.load_manifest(meta["manifest"])
            for path in wanted:
                if path in files and path not in current and path not in chosen:
                    chosen[path] = (files[path], sid)
            if len(chosen) == len(wanted):
                break
        plan = Plan(label="recover")
        for path in sorted(chosen):
            plan.writes.append((path, chosen[path][0], "create"))
        holder["sources"] = {p: s for p, (e, s) in chosen.items()}
        return plan

    label = sorted(wanted)[0] if len(wanted) == 1 else "%d files" % len(wanted)
    res = execute(repo, compute, "recover %s" % label, dry_run)
    res.sources = holder.get("sources", {})
    return res


def deleted_files(repo, include_git=False, max_versions=400):
    """Files that some saved version has but the folder doesn't have now, newest deletion first.

    Returns dicts: path, size, last_seen (snapshot id that still had it), deleted_at (time of the
    first snapshot without it, or None if it vanished after the latest snapshot), deleted_in
    (that snapshot's id) and session (the session that snapshot belongs to).
    """
    from .scan import load_ignore

    current = scan(repo).files
    matcher = load_ignore(repo)
    found = {}
    seen = set()
    newer = None
    checked = 0
    for sid in reversed(repo.snapshot_ids()):
        meta = repo.get_snapshot(sid)
        if meta["manifest"] in seen:
            newer = meta
            continue
        seen.add(meta["manifest"])
        checked += 1
        if checked > max_versions:
            break
        for path, entry in repo.load_manifest(meta["manifest"]).items():
            if path in current or path in found:
                continue
            if not include_git and (path == ".git" or path.startswith(".git/")):
                continue
            if matcher.is_ignored(path):
                continue
            found[path] = {
                "path": path,
                "size": entry.get("s"),
                "link": entry.get("l"),
                "last_seen": sid,
                "last_seen_time": meta["time"],
                "deleted_at": newer["time"] if newer else None,
                "deleted_in": newer["id"] if newer else None,
                "session": newer.get("session") if newer else None,
            }
        newer = meta
    out = list(found.values())
    out.sort(key=lambda d: (-(d["deleted_at"] or 1e18), d["path"]))
    return out
