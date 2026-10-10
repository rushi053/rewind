"""The seatbelt: static detection of destructive shell commands.

This is a heuristic safety net, not a sandbox. It parses the command with
shlex, splits it into simple commands (``;``, ``&&``, ``||``, ``|``), strips
wrappers such as ``sudo``/``env``/``bash -c``, and checks each one against a
set of rules. Anything it cannot see (a script file that runs ``rm -rf``) it
cannot catch -- which is why Rewind also snapshots before every command.
"""

import fnmatch
import os
import re
import shlex
import tempfile

CRITICAL = "critical"
HIGH = "high"

RULES = {
    "rm-root": (CRITICAL, "rm -r on / or a system directory"),
    "rm-home": (CRITICAL, "rm -r on your home directory"),
    "rm-outside": (HIGH, "rm -r on a path outside the project"),
    "rm-project": (HIGH, "rm -r of the whole project directory"),
    "rm-git-dir": (HIGH, "deleting the .git directory"),
    "rm-rewind-store": (HIGH, "deleting Rewind's own snapshot store"),
    "rm-variable": (HIGH, "rm -r on an unexpanded variable (empty variable = rm -rf /)"),
    "delete-env": (HIGH, "deleting a .env file"),
    "overwrite-env": (HIGH, "overwriting an existing .env file"),
    "git-reset-hard": (HIGH, "git reset --hard discards uncommitted work"),
    "git-clean": (HIGH, "git clean -f deletes untracked files"),
    "git-force-push": (HIGH, "force-push to a protected branch"),
    "git-discard": (HIGH, "git checkout/restore that discards local changes"),
    "git-stash-drop": (HIGH, "dropping git stashes"),
    "git-reflog-expire": (HIGH, "expiring the reflog destroys git's own undo history"),
    "find-delete": (HIGH, "find with -delete / -exec rm"),
    "dd": (HIGH, "dd writing to a file or device"),
    "disk-format": (CRITICAL, "formatting or wiping a disk"),
    "device-write": (CRITICAL, "writing directly to a block device"),
    "chmod-777": (HIGH, "recursive chmod 777"),
    "chown-recursive-root": (HIGH, "recursive chown on / or home"),
    "truncate": (HIGH, "truncating a file"),
    "shred": (HIGH, "shredding files"),
    "sql-drop": (HIGH, "DROP DATABASE / TABLE / SCHEMA or TRUNCATE TABLE"),
    "sql-delete-all": (HIGH, "DELETE FROM without WHERE"),
    "db-drop": (HIGH, "dropping or resetting a database"),
    "pipe-to-shell": (HIGH, "piping a download straight into a shell"),
    "fork-bomb": (CRITICAL, "fork bomb"),
    "mv-devnull": (HIGH, "moving files to /dev/null"),
}

SYSTEM_DIRS = {
    "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64", "/opt", "/proc", "/root",
    "/sbin", "/srv", "/sys", "/usr", "/var", "/Applications", "/Library", "/System", "/Users",
    "/Volumes", "/private", "/private/etc", "/private/var",
}
WRAPPERS = {"sudo", "doas", "command", "builtin", "exec", "nohup", "time", "nice", "ionice", "stdbuf", "env", "xargs", "timeout", "caffeinate"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
INTERPRETERS = SHELLS | {"python", "python3", "perl", "ruby", "node", "deno", "bun"}
SEPARATORS = {";", "&&", "||", "|", "&", "|&", "(", ")", ";;", "\n"}
PROTECTED_BRANCHES = {"main", "master", "trunk", "production", "prod", "release"}
SEARCH_TOOLS = {"grep", "egrep", "fgrep", "rg", "ag", "ack", "git", "gh"}

SQL_DROP = re.compile(r"\b(drop\s+(database|table|schema)|truncate\s+table)\b", re.I)
SQL_DELETE_ALL = re.compile(r"\bdelete\s+from\s+[\w.\"`]+\s*(;|$|[\"'])", re.I)
FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:")
SUBST_DOWNLOAD = re.compile(r"(\$\(|`|<\()\s*(curl|wget)\b")
BLOCK_DEVICE = re.compile(r"^/dev/(sd[a-z]|hd[a-z]|nvme\d|disk\d|rdisk\d|mmcblk\d|xvd[a-z]|vd[a-z])")


class Finding:
    def __init__(self, rule, detail=""):
        self.rule = rule
        self.severity, self.reason = RULES[rule]
        self.detail = detail

    def __repr__(self):
        return "Finding(%s, %r)" % (self.rule, self.detail)

    def message(self):
        return self.reason + (" (%s)" % self.detail if self.detail else "")


def _tokenize(cmd):
    cmd = cmd.replace("\r", "")
    try:
        lex = shlex.shlex(cmd.replace("\n", " ; "), posix=True, punctuation_chars=";&|()<>")
        lex.whitespace_split = True
        lex.commenters = ""
        return list(lex)
    except ValueError:
        return cmd.replace(";", " ; ").replace("|", " | ").split()


def split_segments(cmd):
    """Return [(tokens, piped_from_previous)] for each simple command."""
    segments, cur, piped = [], [], False
    for tok in _tokenize(cmd):
        if tok in SEPARATORS or (tok and set(tok) <= set(";&|()") ):
            if cur:
                segments.append((cur, piped))
            piped = tok in ("|", "|&")
            cur = []
        else:
            cur.append(tok)
    if cur:
        segments.append((cur, piped))
    return segments


def _strip_wrappers(tokens):
    toks = list(tokens)
    while toks:
        head = toks[0]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", head):
            toks.pop(0)
            continue
        name = os.path.basename(head)
        if name in WRAPPERS:
            toks.pop(0)
            # drop wrapper options (and the argument of options that take one)
            while toks and (toks[0].startswith("-") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0])
                            or (name == "timeout" and re.match(r"^\d", toks[0]))):
                opt = toks.pop(0)
                if name in ("sudo", "doas") and opt in ("-u", "-g", "-C", "-p") and toks:
                    toks.pop(0)
                if name == "xargs" and opt in ("-I", "-n", "-P", "-L", "-d", "-E", "-s") and toks:
                    toks.pop(0)
            continue
        break
    return toks


class Context:
    def __init__(self, cwd, root, home=None):
        self.cwd = os.path.abspath(cwd or root or os.getcwd())
        self.root = os.path.abspath(root or self.cwd)
        self.home = os.path.abspath(home or os.path.expanduser("~"))
        tmps = {"/tmp", "/private/tmp", "/var/tmp", "/var/folders", "/private/var/folders", tempfile.gettempdir()}
        self.tmps = {os.path.realpath(t) for t in tmps} | {os.path.abspath(t) for t in tmps}

    def expand(self, path):
        if path == "~" or path.startswith("~/"):
            return self.home + path[1:]
        for var in ("$HOME", "${HOME}"):
            if path == var or path.startswith(var + "/"):
                return self.home + path[len(var):]
        return path

    def resolve(self, path):
        p = self.expand(path)
        if not os.path.isabs(p):
            p = os.path.join(self.cwd, p)
        return os.path.normpath(p)

    def inside(self, path, base):
        return path == base or path.startswith(base.rstrip(os.sep) + os.sep)

    def in_tmp(self, path):
        return any(self.inside(path, t) and path != t for t in self.tmps)


def _is_env_name(path):
    base = os.path.basename(path.rstrip("/"))
    if not (base == ".env" or base.startswith(".env.")):
        return False
    return not base.endswith((".example", ".sample", ".template", ".dist"))


def _check_rm(args, ctx, out):
    flags, targets, end = "", [], False
    for a in args:
        if not end and a == "--":
            end = True
        elif not end and a.startswith("--"):
            flags += {"--recursive": "r", "--force": "f"}.get(a, "")
        elif not end and a.startswith("-") and len(a) > 1:
            flags += a[1:]
        else:
            targets.append(a)
    recursive = "r" in flags or "R" in flags
    for t in targets:
        if _is_env_name(t):
            out.append(Finding("delete-env", t))
        if not recursive:
            continue
        stripped = t.rstrip("/") or "/"
        base = stripped[:-2] if stripped.endswith("/*") else stripped
        if base in ("*", ".*", ""):
            base = "." if base else "/"
        if re.match(r"^\$\{?[A-Za-z_][A-Za-z0-9_]*\}?$", base) and base not in ("$HOME", "${HOME}"):
            out.append(Finding("rm-variable", t))
            continue
        target = ctx.resolve(base)
        if target == "/" or target in SYSTEM_DIRS:
            out.append(Finding("rm-root", t))
        elif target == ctx.home:
            out.append(Finding("rm-home", t))
        elif os.path.basename(target) == ".rewind" or (os.sep + ".rewind" + os.sep) in target + os.sep:
            out.append(Finding("rm-rewind-store", t))
        elif ctx.inside(target, ctx.root) and (os.path.basename(target) == ".git" or (os.sep + ".git" + os.sep) in target + os.sep):
            out.append(Finding("rm-git-dir", t))
        elif target == ctx.root:
            out.append(Finding("rm-project", t))
        elif ctx.inside(ctx.root, target):
            out.append(Finding("rm-outside", t + " (contains the project)"))
        elif not ctx.inside(target, ctx.root) and not ctx.in_tmp(target):
            out.append(Finding("rm-outside", t))


def _git_subcommand(args):
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a, args[i + 1:]
    return None, []


def _check_git(args, ctx, out):
    sub, rest = _git_subcommand(args)
    if sub is None:
        return
    opts = [a for a in rest if a.startswith("-")]
    pos = [a for a in rest if not a.startswith("-")]
    short = "".join(a[1:] for a in opts if not a.startswith("--"))
    if sub == "reset" and "--hard" in opts:
        out.append(Finding("git-reset-hard", " ".join(["git", sub] + rest)))
    elif sub == "clean":
        if ("f" in short or "--force" in opts) and not ("n" in short or "--dry-run" in opts):
            out.append(Finding("git-clean", " ".join(["git", sub] + rest)))
    elif sub == "push":
        force = "f" in short or "--force" in opts or any(p.startswith("+") for p in pos)
        lease = any(o.startswith("--force-with-lease") or o == "--force-if-includes" for o in opts)
        refs = pos[1:]
        targets = set()
        for r in refs:
            r = r.lstrip("+")
            targets.add(r.split(":")[-1].replace("refs/heads/", ""))
        protected = targets & PROTECTED_BRANCHES
        if (force or lease) and protected:
            out.append(Finding("git-force-push", "to " + ", ".join(sorted(protected))))
        elif force and not refs and not lease:
            out.append(Finding("git-force-push", "no branch given; may push the current branch with --force"))
        if "--mirror" in opts:
            out.append(Finding("git-force-push", "--mirror"))
    elif sub in ("checkout", "restore"):
        after_dd = rest[rest.index("--") + 1:] if "--" in rest else pos
        if sub == "restore" and ("--staged" in opts or "S" in short) and not ("--worktree" in opts or "W" in short):
            return
        if any(p in (".", "./", ":/", "*", ":/*") for p in after_dd):
            out.append(Finding("git-discard", " ".join(["git", sub] + rest)))
        elif sub == "checkout" and ("-f" in opts or "--force" in opts):
            out.append(Finding("git-discard", "git checkout --force"))
    elif sub == "stash" and pos[:1] and pos[0] in ("drop", "clear"):
        out.append(Finding("git-stash-drop", "git stash " + pos[0]))
    elif sub == "reflog" and pos[:1] == ["expire"] and any("--expire=now" in o or "--expire=all" in o for o in opts):
        out.append(Finding("git-reflog-expire"))


def _check_redirects(tokens, ctx, out):
    for i, tok in enumerate(tokens):
        if tok in (">", ">|") and i + 1 < len(tokens):
            target = tokens[i + 1]
            if BLOCK_DEVICE.match(target):
                out.append(Finding("device-write", target))
            elif _is_env_name(target) and os.path.exists(ctx.resolve(target)):
                out.append(Finding("overwrite-env", target))


def check_segment(tokens, ctx, out, depth=0):
    _check_redirects(tokens, ctx, out)
    toks = _strip_wrappers([t for t in tokens if t not in (">", ">>", "<", ">|")])
    if not toks:
        return None
    name = os.path.basename(toks[0])
    args = toks[1:]
    if name in SHELLS or name == "eval":
        script = None
        if name == "eval":
            script = " ".join(args)
        else:
            for i, a in enumerate(args):
                if a == "-c" or (a.startswith("-") and not a.startswith("--") and "c" in a[1:]):
                    if i + 1 < len(args):
                        script = args[i + 1]
                    break
        if script and depth < 4:
            out.extend(check(script, ctx.cwd, ctx.root, depth=depth + 1, home=ctx.home))
        return name
    if name == "cd":
        # track directory changes so `cd .. && rm -rf project` is understood
        target = next((a for a in args if not a.startswith("-")), "~")
        ctx.cwd = ctx.resolve(target)
        return name
    if name == "rm":
        _check_rm(args, ctx, out)
    elif name == "git":
        _check_git(args, ctx, out)
    elif name == "find":
        if "-delete" in args:
            out.append(Finding("find-delete", "-delete"))
        for flag in ("-exec", "-execdir", "-ok"):
            if flag in args:
                i = args.index(flag)
                if i + 1 < len(args) and os.path.basename(args[i + 1]) in ("rm", "shred", "unlink"):
                    out.append(Finding("find-delete", flag + " " + args[i + 1]))
    elif name == "dd":
        for a in args:
            if a.startswith("of="):
                tgt = a[3:]
                out.append(Finding("device-write" if BLOCK_DEVICE.match(tgt) else "dd", a))
    elif name.startswith("mkfs") or name in ("mke2fs", "newfs", "wipefs", "mkswap"):
        out.append(Finding("disk-format", name))
    elif name == "diskutil" and args and args[0].lower() in ("erasedisk", "erasevolume", "zerodisk", "secureerase", "partitiondisk"):
        out.append(Finding("disk-format", "diskutil " + args[0]))
    elif name == "chmod":
        rec = any(a in ("-R", "--recursive") or (a.startswith("-") and not a.startswith("--") and "R" in a) for a in args)
        if rec and any(a in ("777", "0777", "a+rwx", "ugo+rwx", "o+w", "a+w") for a in args):
            out.append(Finding("chmod-777", " ".join(args)))
    elif name == "chown":
        rec = any(a in ("-R", "--recursive") or (a.startswith("-") and not a.startswith("--") and "R" in a) for a in args)
        if rec:
            for a in args:
                if not a.startswith("-") and ctx.resolve(a) in (SYSTEM_DIRS | {ctx.home}) and a != args[0]:
                    out.append(Finding("chown-recursive-root", a))
    elif name == "truncate":
        out.append(Finding("truncate", " ".join(args)))
    elif name == "shred":
        out.append(Finding("shred", " ".join(args)))
    elif name in ("dropdb",) or (name in ("rails", "rake", "bin/rails") and any(a in ("db:drop", "db:reset", "db:schema:load") for a in args)):
        out.append(Finding("db-drop", " ".join([name] + args)))
    elif name in ("prisma", "npx", "bunx", "pnpm", "yarn") and "migrate" in args and ("reset" in args or "--force-reset" in args):
        out.append(Finding("db-drop", " ".join([name] + args)))
    elif name == "mv" and args and args[-1] == "/dev/null":
        out.append(Finding("mv-devnull", " ".join(args)))
    elif name in ("cp", "mv", "install") and len(args) >= 2 and not args[-1].startswith("-"):
        dest = args[-1]
        if _is_env_name(dest) and os.path.exists(ctx.resolve(dest)) and "-n" not in args and "--no-clobber" not in args:
            out.append(Finding("overwrite-env", dest))
    elif name == "tee" and "-a" not in args and "--append" not in args:
        for a in args:
            if _is_env_name(a) and os.path.exists(ctx.resolve(a)):
                out.append(Finding("overwrite-env", a))
    if name not in SEARCH_TOOLS:
        text = " ".join(toks)
        if SQL_DROP.search(text):
            out.append(Finding("sql-drop", SQL_DROP.search(text).group(0)))
        elif SQL_DELETE_ALL.search(text) and not re.search(r"\bwhere\b", text, re.I):
            out.append(Finding("sql-delete-all", SQL_DELETE_ALL.search(text).group(0).strip(" ;\"'")))
    return name


def check(command, cwd=None, root=None, depth=0, home=None):
    """Return a list of Findings for a shell command string (empty = looks safe)."""
    ctx = Context(cwd, root, home)
    out = []
    if FORK_BOMB.search(command):
        out.append(Finding("fork-bomb"))
    if SUBST_DOWNLOAD.search(command) and re.search(r"\b(ba|z|da|k)?sh\b|\bsource\b|\beval\b", command):
        out.append(Finding("pipe-to-shell", "shell runs a downloaded script"))
    prev_name = None
    for tokens, piped in split_segments(command):
        name = check_segment(tokens, ctx, out, depth)
        if piped and prev_name in ("curl", "wget") and name in INTERPRETERS:
            out.append(Finding("pipe-to-shell", "%s | %s" % (prev_name, name)))
        prev_name = name
    # de-duplicate by rule+detail, keep order
    seen, unique = set(), []
    for f in out:
        key = (f.rule, f.detail)
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def evaluate(command, config, cwd=None, root=None):
    """Apply allowlist/disabled rules. Returns (findings, decision) where decision is
    'allow' | 'block' | 'snapshot' | 'warn'."""
    mode = (config.get("guard.mode") or "block") if config is not None else "block"
    if mode == "off":
        return [], "allow"
    allow = list(config.get("guard.allow") or []) if config is not None else []
    disabled = set(config.get("guard.disable") or []) if config is not None else set()
    stripped = command.strip()
    for pat in allow:
        if fnmatch.fnmatchcase(stripped, pat) or stripped == pat:
            return [], "allow"
    findings = [f for f in check(command, cwd, root) if f.rule not in disabled]
    if not findings:
        return [], "allow"
    if any(f.severity == CRITICAL for f in findings):
        return findings, "block"
    return findings, mode
