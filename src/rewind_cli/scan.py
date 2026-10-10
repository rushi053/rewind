"""Walk the project tree, hashing only files whose stat signature changed."""

import hashlib
import os
import stat as statmod
import time

from .ignore import DEFAULT_IGNORE, IgnoreMatcher
from .store import read_json, write_json

RACY_WINDOW = 2.0  # seconds; files modified this recently are re-hashed next time


class ScanResult:
    def __init__(self, files, skipped, errors, hashed):
        self.files = files
        self.skipped = skipped  # [(path, size)] over the size limit
        self.errors = errors  # [(path, message)]
        self.hashed = hashed  # number of files actually read this scan


def load_ignore(repo):
    m = IgnoreMatcher()
    f = repo.root / ".rewindignore"
    try:
        m.add_text(f.read_text("utf-8"))
    except FileNotFoundError:
        m.add_text(DEFAULT_IGNORE)
    except (OSError, UnicodeDecodeError):
        m.add_text(DEFAULT_IGNORE)
    m.add_text(".rewind/")
    if not repo.config.get("include_git", True):
        m.add_text(".git/")
    return m


def walk(repo, matcher=None, errors=None):
    """Yield (relpath, os.stat_result, is_symlink) for every non-ignored file."""
    matcher = matcher or load_ignore(repo)
    root = str(repo.root)
    store = os.path.normcase(os.path.abspath(str(repo.store)))
    stack = [("", root)]
    while stack:
        rel_dir, abs_dir = stack.pop()
        try:
            it = os.scandir(abs_dir)
        except OSError as exc:
            if errors is not None:
                errors.append((rel_dir or ".", str(exc)))
            continue
        with it:
            entries = sorted(it, key=lambda e: e.name)
        for entry in entries:
            rel = entry.name if not rel_dir else rel_dir + "/" + entry.name
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                if errors is not None:
                    errors.append((rel, str(exc)))
                continue
            mode = st.st_mode
            if statmod.S_ISDIR(mode):
                if matcher.match_one(rel, True):  # parents already passed
                    continue
                if os.path.normcase(os.path.abspath(entry.path)) == store:
                    continue
                stack.append((rel, entry.path))
            elif statmod.S_ISREG(mode) or statmod.S_ISLNK(mode):
                if matcher.match_one(rel, False):
                    continue
                yield rel, st, statmod.S_ISLNK(mode)
            # sockets, fifos, devices are skipped


def fingerprint(repo, matcher=None):
    """Cheap signature of the tree from stat data only (used by the watcher)."""
    h = hashlib.sha1()
    for rel, st, is_link in walk(repo, matcher):
        h.update(("%s\0%d\0%d\0%d\n" % (rel, st.st_size, st.st_mtime_ns, st.st_mode)).encode("utf-8", "surrogateescape"))
    return h.hexdigest()


def hash_file(path):
    with open(path, "rb") as fh:
        data = fh.read()
    return hashlib.sha256(data).hexdigest(), data


def scan(repo):
    """Return a ScanResult; new file contents are written to the object store."""
    with repo.lock:
        return _scan(repo)


def _scan(repo):
    limit = int(float(repo.config.get("max_file_size_mb", 50)) * 1024 * 1024)
    index_path = repo.store / "index.json"
    index = read_json(index_path, {}) or {}
    new_index = {}
    files, skipped, errors = {}, [], []
    hashed = 0
    now = time.time()
    root = str(repo.root)
    for rel, st, is_link in walk(repo, errors=errors):
        abspath = os.path.join(root, *rel.split("/"))
        if is_link:
            try:
                files[rel] = {"l": os.readlink(abspath)}
            except OSError as exc:
                errors.append((rel, str(exc)))
            continue
        if st.st_size > limit:
            skipped.append((rel, st.st_size))
            continue
        perm = statmod.S_IMODE(st.st_mode)
        sig = [st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino]
        cached = index.get(rel)
        sha = None
        if cached and cached[:4] == sig and not (len(cached) > 6 and cached[6]):
            sha = cached[4]
        if sha is None:
            try:
                sha, data = hash_file(abspath)
            except OSError as exc:
                errors.append((rel, str(exc)))
                continue
            repo.put_bytes(data, sha)
            hashed += 1
        racy = (now - st.st_mtime_ns / 1e9) < RACY_WINDOW
        new_index[rel] = sig + [sha, perm] + ([1] if racy else [])
        files[rel] = {"h": sha, "m": perm, "s": st.st_size}
    if new_index != index:
        write_json(index_path, new_index)
    return ScanResult(files, skipped, errors, hashed)


def update_index(repo, entries):
    """Record freshly written files so the next scan does not re-hash them."""
    index_path = repo.store / "index.json"
    index = read_json(index_path, {}) or {}
    now = time.time()
    for rel, sha in entries:
        abspath = os.path.join(str(repo.root), *rel.split("/"))
        try:
            st = os.lstat(abspath)
        except OSError:
            index.pop(rel, None)
            continue
        racy = (now - st.st_mtime_ns / 1e9) < RACY_WINDOW
        index[rel] = [st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino, sha, statmod.S_IMODE(st.st_mode)] + ([1] if racy else [])
    write_json(index_path, index)
