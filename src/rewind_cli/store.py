"""The content-addressed snapshot store.

Layout of a store directory (``<project>/.rewind`` or ``~/.rewind/projects/<hash>``)::

    config.json          user configuration (merged over defaults)
    project.json         {"root": "/abs/path/to/project"}
    objects/ab/cdef...   zlib-compressed file contents, keyed by sha256 of the raw bytes
    snapshots/00000042.json  snapshot metadata; points at a manifest object
    sessions/s7.json     agent sessions (rewind run, hook sessions)
    index.json           stat cache: path -> [size, mtime_ns, ctime_ns, ino, sha, mode]
    counters.json        last snapshot / session ids and the HEAD manifest
    state.json           undo/redo cursor
    lock                 inter-process lock file

A manifest is itself an object: canonical JSON mapping each relative path to
``{"h": sha, "m": mode, "s": size}`` (regular file) or ``{"l": target}`` (symlink).
Identical trees therefore share a manifest and unchanged files cost nothing.
"""

import errno
import hashlib
import json
import os
import random
import threading
import time
import zlib
from pathlib import Path

from .config import Config

STORE_DIRNAME = ".rewind"
MANIFEST_CACHE_SIZE = 64


class RewindError(Exception):
    """An error caused by user input or project state; printed without a traceback."""


class NotInitialized(RewindError):
    pass


def rewind_home():
    return Path(os.environ.get("REWIND_HOME") or (Path.home() / ".rewind"))


def project_hash(root):
    return hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:16]


def _atomic_write(path, data, mode=None):
    tmp = path.with_name(".%s.tmp-%d-%d" % (path.name, os.getpid(), random.randint(0, 1 << 30)))
    with open(str(tmp), "wb") as fh:
        fh.write(data)
    if mode is not None:
        os.chmod(str(tmp), mode)
    os.replace(str(tmp), str(path))


def write_json(path, obj, indent=None):
    data = json.dumps(obj, indent=indent, sort_keys=True, separators=None if indent else (",", ":"))
    _atomic_write(path, data.encode("utf-8"))


def read_json(path, default=None):
    try:
        with open(str(path), "rb") as fh:
            return json.loads(fh.read().decode("utf-8"))
    except (OSError, ValueError):
        return default


def _pid_alive(pid):
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


class Lock:
    """Re-entrant (per process) exclusive lock using an O_EXCL lock file."""

    def __init__(self, path, timeout=30.0):
        self.path = path
        self.timeout = timeout
        self.depth = 0
        self._thread_lock = threading.RLock()

    def acquire(self):
        self._thread_lock.acquire()
        if self.depth:
            self.depth += 1
            return
        deadline = time.time() + self.timeout
        delay = 0.005
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self.depth = 1
                return
            except FileExistsError:
                if self._stale():
                    try:
                        os.unlink(str(self.path))
                    except OSError:
                        pass
                    continue
                if time.time() > deadline:
                    self._thread_lock.release()
                    raise RewindError("store is locked by another rewind process (%s)" % self.path)
                time.sleep(delay)
                delay = min(delay * 2, 0.1)

    def _stale(self):
        """A lock is stale only when the process that wrote it is gone.

        Age is not a reason to steal. A snapshot of a large tree can hold the
        lock for longer than any fixed cutoff, and taking it from a live pid
        lets two processes write the store at once.
        """
        try:
            pid = int(self.path.read_text().strip() or "0")
        except (OSError, ValueError):
            return False
        return not _pid_alive(pid)

    def release(self):
        self.depth -= 1
        if self.depth == 0:
            try:
                os.unlink(str(self.path))
            except OSError:
                pass
        self._thread_lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


class Repo:
    def __init__(self, root, store):
        self.root = Path(root)
        self.store = Path(store)
        self.objects = self.store / "objects"
        self.snapdir = self.store / "snapshots"
        self.sessdir = self.store / "sessions"
        self.lock = Lock(self.store / "lock")
        self._config = None
        self._manifest_cache = {}

    # ------------------------------------------------------------------ setup
    @classmethod
    def find(cls, start=None):
        start = Path(start or os.getcwd()).resolve()
        home = rewind_home()
        for d in [start] + list(start.parents):
            local = d / STORE_DIRNAME
            if (local / "project.json").exists():
                return cls(d, local)
            glob = home / "projects" / project_hash(d)
            if (glob / "project.json").exists():
                return cls(d, glob)
        raise NotInitialized("not a rewind project (run `rewind init` here first)")

    @classmethod
    def init(cls, root, global_store=None):
        """Create or reuse the store for ``root``.

        New projects keep history in ``~/.rewind/projects/<hash>`` so copies of
        ``.env`` are not sitting inside the tree an agent can read. Pass
        ``global_store=False`` to opt into ``<project>/.rewind`` instead.

        An existing store is always reused. An older in-project ``.rewind`` is
        kept (that is what ``find`` would open); history is not split across
        two directories and nothing is migrated out from under the user.
        ``store_note`` on the result is ``created-home``, ``created-local``,
        ``reused-home``, or ``reused-local``.
        """
        root = Path(root).resolve()
        local = root / STORE_DIRNAME
        home = rewind_home() / "projects" / project_hash(root)
        local_ready = (local / "project.json").exists()
        home_ready = (home / "project.json").exists()
        if local_ready:
            # find() checks the in-project store first. Keep using it.
            store, note = local, "reused-local"
        elif home_ready:
            store, note = home, "reused-home"
        elif global_store is False:
            store, note = local, "created-local"
        else:
            store, note = home, "created-home"
        for sub in ("objects", "snapshots", "sessions"):
            (store / sub).mkdir(parents=True, exist_ok=True)
        if not (store / "project.json").exists():
            write_json(store / "project.json", {"root": str(root), "created": time.time(), "version": 1}, indent=2)
        if not (store / "config.json").exists():
            (store / "config.json").write_text("{}\n", "utf-8")
        if store == local:
            gi = store / ".gitignore"
            if not gi.exists():
                gi.write_text("# Rewind snapshot store -- never commit this.\n*\n", "utf-8")
        repo = cls(root, store)
        repo.store_note = note
        return repo

    @property
    def is_global(self):
        return not self._inside(self.store, self.root)

    @staticmethod
    def _inside(path, parent):
        try:
            Path(path).resolve().relative_to(Path(parent).resolve())
            return True
        except ValueError:
            return False

    @property
    def config(self):
        if self._config is None:
            self._config = Config(self.store / "config.json")
        return self._config

    def reload_config(self):
        self._config = None

    # ---------------------------------------------------------------- objects
    def object_path(self, sha):
        return self.objects / sha[:2] / sha[2:]

    def has_object(self, sha):
        return self.object_path(sha).exists()

    def put_bytes(self, data, sha=None):
        sha = sha or hashlib.sha256(data).hexdigest()
        path = self.object_path(sha)
        if not path.exists():
            path.parent.mkdir(exist_ok=True)
            _atomic_write(path, zlib.compress(data, 6))
        return sha

    def get_bytes(self, sha):
        try:
            with open(str(self.object_path(sha)), "rb") as fh:
                return zlib.decompress(fh.read())
        except FileNotFoundError:
            raise RewindError("object %s is missing from the store (run `rewind doctor`)" % sha[:12])

    def iter_objects(self):
        if not self.objects.exists():
            return
        for sub in self.objects.iterdir():
            if sub.is_dir() and len(sub.name) == 2:
                for f in sub.iterdir():
                    if not f.name.startswith("."):
                        yield sub.name + f.name, f

    # -------------------------------------------------------------- manifests
    def put_manifest(self, files):
        data = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return self.put_bytes(data)

    def load_manifest(self, sha):
        cached = self._manifest_cache.get(sha)
        if cached is not None:
            return cached
        files = json.loads(self.get_bytes(sha).decode("utf-8"))
        if len(self._manifest_cache) >= MANIFEST_CACHE_SIZE:
            self._manifest_cache.pop(next(iter(self._manifest_cache)))
        self._manifest_cache[sha] = files
        return files

    # --------------------------------------------------------------- counters
    def counters(self):
        return read_json(self.store / "counters.json", {}) or {}

    def _save_counters(self, c):
        write_json(self.store / "counters.json", c)

    def head(self):
        """(snapshot id, manifest sha) of the latest snapshot, or (None, None)."""
        c = self.counters()
        return c.get("head"), c.get("head_manifest")

    # -------------------------------------------------------------- snapshots
    def snap_path(self, sid):
        return self.snapdir / ("%08d.json" % int(sid))

    def get_snapshot(self, sid):
        meta = read_json(self.snap_path(sid))
        if meta is None:
            raise RewindError("no snapshot #%s" % sid)
        return meta

    def snapshot_ids(self):
        ids = []
        if self.snapdir.exists():
            for f in self.snapdir.iterdir():
                if f.suffix == ".json" and f.stem.isdigit():
                    ids.append(int(f.stem))
        ids.sort()
        return ids

    def list_snapshots(self):
        out = []
        for sid in self.snapshot_ids():
            meta = read_json(self.snap_path(sid))
            if meta:
                out.append(meta)
        return out

    def snapshot_files(self, meta_or_id):
        meta = meta_or_id if isinstance(meta_or_id, dict) else self.get_snapshot(meta_or_id)
        return self.load_manifest(meta["manifest"])

    def resolve(self, ref):
        """Resolve '12', '#12', 'HEAD', 'latest', 'HEAD~2' to a snapshot id."""
        ref = str(ref).strip()
        ids = self.snapshot_ids()
        if not ids:
            raise RewindError("no snapshots yet (take one with `rewind snap`)")
        r = ref.lstrip("#")
        if r.isdigit():
            sid = int(r)
            if sid not in ids:
                raise RewindError("no snapshot #%d (latest is #%d)" % (sid, ids[-1]))
            return sid
        low = r.lower()
        back = 0
        for name in ("head", "latest", "last"):
            if low == name:
                return ids[-1]
            if low.startswith(name + "~"):
                try:
                    back = int(low[len(name) + 1:] or "1")
                except ValueError:
                    break
                if back >= len(ids):
                    raise RewindError("%s goes past the first snapshot" % ref)
                return ids[-1 - back]
        raise RewindError("not a snapshot reference: %r (use a number like 12, or HEAD~1)" % ref)

    def capture(self, message="", trigger="manual", session=None, tool=None, detail=None, force=False, files=None):
        """Scan the working tree and record a snapshot if anything changed.

        Returns ``(meta_or_None, files, head_id)``. ``meta`` is None when the tree
        matches the latest snapshot (a no-op), in which case ``head_id`` is the
        snapshot that already represents the current state.
        """
        from .scan import scan
        from .diff import diff_manifests, summarize

        with self.lock:
            skipped = 0
            if files is None:
                result = scan(self)
                files = result.files
                skipped = len(result.skipped)
            manifest = self.put_manifest(files)
            c = self.counters()
            head, head_manifest = c.get("head"), c.get("head_manifest")
            if head is not None and manifest == head_manifest and not force:
                return None, files, head
            parent_files = self.load_manifest(head_manifest) if head_manifest else {}
            changes = diff_manifests(parent_files, files)
            sid = int(c.get("snap", 0)) + 1
            meta = {
                "id": sid,
                "time": time.time(),
                "manifest": manifest,
                "parent": head,
                "message": message or "",
                "trigger": trigger,
                "session": session,
                "tool": tool,
                "detail": (detail or "")[:300] or None,
                "stats": summarize(changes),
                "changes": [[c_["status"], c_["path"]] + ([c_["old_path"]] if c_.get("old_path") else []) for c_ in changes[:500]],
                "nchanges": len(changes),
                "files": len(files),
                "bytes": sum(e.get("s", 0) for e in files.values()),
                "skipped": skipped,
            }
            write_json(self.snap_path(sid), meta)
            c.update({"snap": sid, "head": sid, "head_manifest": manifest})
            self._save_counters(c)
            if session:
                self.touch_session(session, sid)
            return meta, files, sid

    def delete_snapshot(self, sid):
        try:
            os.unlink(str(self.snap_path(sid)))
        except FileNotFoundError:
            pass

    # --------------------------------------------------------------- sessions
    def new_session(self, name, agent=None, command=None, external=None):
        with self.lock:
            c = self.counters()
            n = int(c.get("session", 0)) + 1
            c["session"] = n
            self._save_counters(c)
            sid = "s%d" % n
            now = time.time()
            sess = {
                "id": sid,
                "name": name,
                "agent": agent,
                "command": command,
                "external": external,
                "started": now,
                "last": now,
                "ended": None,
                "exit_code": None,
                "start_snapshot": None,
                "end_snapshot": None,
                "snapshots": [],
            }
            write_json(self.sessdir / (sid + ".json"), sess)
            if external:
                ext = read_json(self.sessdir / "external.json", {}) or {}
                ext[external] = sid
                write_json(self.sessdir / "external.json", ext)
            return sess

    def get_session(self, sid):
        sid = str(sid)
        if not sid.startswith("s"):
            sid = "s" + sid
        sess = read_json(self.sessdir / (sid + ".json"))
        if sess is None:
            raise RewindError("no session %s" % sid)
        return sess

    def save_session(self, sess):
        write_json(self.sessdir / (sess["id"] + ".json"), sess)

    def session_for_external(self, external):
        ext = read_json(self.sessdir / "external.json", {}) or {}
        sid = ext.get(external)
        if sid:
            try:
                return self.get_session(sid)
            except RewindError:
                return None
        return None

    def list_sessions(self):
        out = []
        if self.sessdir.exists():
            for f in self.sessdir.iterdir():
                if f.suffix == ".json" and f.stem.startswith("s") and f.stem[1:].isdigit():
                    s = read_json(f)
                    if s:
                        out.append(s)
        out.sort(key=lambda s: int(s["id"][1:]))
        return out

    def latest_session(self):
        sessions = self.list_sessions()
        return sessions[-1] if sessions else None

    def touch_session(self, sid, snap_id):
        try:
            sess = self.get_session(sid)
        except RewindError:
            return
        sess["last"] = time.time()
        if snap_id is not None and snap_id not in sess["snapshots"]:
            sess["snapshots"].append(snap_id)
        self.save_session(sess)

    # ------------------------------------------------------------------ state
    def state(self):
        return read_json(self.store / "state.json", {}) or {}

    def save_state(self, st):
        write_json(self.store / "state.json", st)

    # ------------------------------------------------------------------ paths
    def rel(self, path, cwd=None):
        """Turn a user-supplied path (relative to cwd) into a project-relative posix path."""
        p = Path(path)
        if not p.is_absolute():
            p = Path(cwd or os.getcwd()) / p
        p = Path(os.path.normpath(str(p)))
        root = Path(os.path.normpath(str(self.root.resolve())))
        try:
            rel = p.relative_to(root)
        except ValueError:
            try:
                rel = p.resolve().relative_to(root)
            except ValueError:
                raise RewindError("%s is outside the project (%s)" % (path, self.root))
        s = rel.as_posix()
        return "" if s == "." else s

    def store_size(self):
        total = 0
        count = 0
        for _, f in self.iter_objects():
            try:
                total += f.stat().st_size
                count += 1
            except OSError:
                pass
        return count, total
