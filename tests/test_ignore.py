import pytest

from rewind_cli.ignore import DEFAULT_IGNORE, IgnoreMatcher


@pytest.mark.parametrize("pattern,path,is_dir,expected", [
    ("node_modules/", "node_modules", True, True),
    ("node_modules/", "a/b/node_modules", True, True),
    ("node_modules/", "node_modules", False, False),  # dir-only rule
    ("*.pyc", "x/y/z.pyc", False, True),
    ("*.pyc", "x/y/z.py", False, False),
    ("/build", "build", True, True),
    ("/build", "src/build", True, False),  # anchored
    ("docs/*.md", "docs/a.md", False, True),
    ("docs/*.md", "docs/sub/a.md", False, False),
    ("docs/**/*.md", "docs/sub/deep/a.md", False, True),
    ("**/logs", "a/b/logs", True, True),
    ("a/**", "a/b/c", False, True),
    ("file?.txt", "file1.txt", False, True),
    ("file[0-9].txt", "file5.txt", False, True),
    ("file[!0-9].txt", "file5.txt", False, False),
])
def test_patterns(pattern, path, is_dir, expected):
    assert IgnoreMatcher(pattern).match_one(path, is_dir) is expected


def test_negation_last_rule_wins():
    m = IgnoreMatcher("*.log\n!keep.log\n")
    assert m.match_one("x.log", False)
    assert not m.match_one("keep.log", False)


def test_comments_and_blank_lines():
    m = IgnoreMatcher("# comment\n\n   \n*.tmp\n")
    assert len(m.rules) == 1


def test_is_ignored_checks_parents():
    m = IgnoreMatcher("build/\n")
    assert m.is_ignored("build/out/x.o")
    assert not m.is_ignored("src/x.c")


def test_defaults_keep_env_and_ignore_deps():
    m = IgnoreMatcher(DEFAULT_IGNORE)
    assert not m.is_ignored(".env")
    assert not m.is_ignored(".git/HEAD")
    assert m.is_ignored("node_modules/react/index.js")
    assert m.is_ignored("web/node_modules/x.js")
    assert m.is_ignored(".venv/bin/python")
    assert m.is_ignored(".git/index.lock")
    assert m.is_ignored("pkg/__pycache__/m.cpython-312.pyc")
