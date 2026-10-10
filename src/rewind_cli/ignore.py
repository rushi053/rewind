"""A small, dependency-free implementation of gitignore-style matching."""

import re

DEFAULT_IGNORE = """\
# .rewindignore -- gitignore syntax. Paths matched here are never snapshotted.
# Unlike git, Rewind DOES snapshot untracked files such as .env by default;
# that is the point. Only list things that are cheap to regenerate.

# dependencies
node_modules/
bower_components/
.venv/
venv/
.tox/
.nox/
vendor/bundle/
.yarn/cache/
.pnpm-store/

# caches
__pycache__/
*.pyc
.mypy_cache/
.pytest_cache/
.ruff_cache/
.cache/
.parcel-cache/
.turbo/
.gradle/
.terraform/

# build output
dist/
build/
target/
.next/
.nuxt/
.svelte-kit/
coverage/
*.egg-info/

# OS noise
.DS_Store
Thumbs.db

# git internals that are rebuilt automatically or are transient
.git/*.lock
.git/**/*.lock
.git/fsmonitor--daemon/
"""


class Rule:
    __slots__ = ("pattern", "regex", "negate", "dir_only")

    def __init__(self, pattern, regex, negate, dir_only):
        self.pattern = pattern
        self.regex = regex
        self.negate = negate
        self.dir_only = dir_only


def _translate_glob(pat):
    """Translate a gitignore glob (without anchoring logic) to a regex body."""
    i, n = 0, len(pat)
    out = []
    while i < n:
        c = pat[i]
        if c == "*":
            if pat[i:i + 2] == "**":
                # '**/' -> zero or more directories, '/**' at end -> everything
                if pat[i:i + 3] == "**/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = i + 1
            if j < n and pat[j] in "!^":
                j += 1
            if j < n and pat[j] == "]":
                j += 1
            while j < n and pat[j] != "]":
                j += 1
            if j >= n:
                out.append("\\[")
            else:
                body = pat[i + 1:j]
                if body and body[0] in "!^":
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j
        elif c == "\\" and i + 1 < n:
            i += 1
            out.append(re.escape(pat[i]))
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def compile_rule(line):
    line = line.rstrip("\n").rstrip("\r")
    if not line.strip() or line.lstrip().startswith("#"):
        return None
    # trailing spaces are ignored unless escaped
    stripped = line.rstrip(" ")
    if stripped.endswith("\\") and len(stripped) < len(line):
        stripped += " "
    line = stripped
    negate = False
    if line.startswith("!"):
        negate = True
        line = line[1:]
    elif line.startswith("\\!") or line.startswith("\\#"):
        line = line[1:]
    dir_only = line.endswith("/")
    line = line.rstrip("/")
    if not line:
        return None
    anchored = "/" in line
    line = line.lstrip("/")
    body = _translate_glob(line)
    prefix = "" if anchored else "(?:.*/)?"
    regex = re.compile("^" + prefix + body + "$")
    return Rule(line, regex, negate, dir_only)


class IgnoreMatcher:
    def __init__(self, text=""):
        self.rules = []
        self.add_text(text)

    def add_text(self, text):
        for line in text.splitlines():
            rule = compile_rule(line)
            if rule is not None:
                self.rules.append(rule)

    def match_one(self, relpath, is_dir):
        """Does this exact path match (ignoring parents)? Last matching rule wins."""
        result = False
        for rule in self.rules:
            if rule.dir_only and not is_dir:
                continue
            if rule.regex.match(relpath):
                result = not rule.negate
        return result

    def is_ignored(self, relpath, is_dir=False):
        """Full check: a path is ignored if it or any parent directory is ignored."""
        parts = relpath.split("/")
        for i in range(1, len(parts)):
            if self.match_one("/".join(parts[:i]), True):
                return True
        return self.match_one(relpath, is_dir)
