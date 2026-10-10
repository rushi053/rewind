import os
import subprocess
import sys

import pytest

from rewind_cli import term
from rewind_cli.cli import main
from rewind_cli.store import Repo


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("REWIND_HOME", str(tmp_path / "rewind-home"))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("REWIND_SESSION", raising=False)
    monkeypatch.setenv("REWIND_NO_NOTIFY", "1")
    term.set_color(None)
    yield
    term.set_color(None)


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('hello')\n")
    (root / "README.md").write_text("# demo\n")
    (root / ".env").write_text("SECRET=abc\n")
    monkeypatch.chdir(root)
    repo = Repo.init(root)
    return repo


@pytest.fixture
def cli(capsys):
    def run(*args):
        code = main([str(a) for a in args])
        out, err = capsys.readouterr()
        return code, out, err

    return run


def write(repo, rel, text, mode=None):
    p = repo.root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        p.write_bytes(text)
    else:
        p.write_text(text)
    if mode is not None:
        os.chmod(str(p), mode)
    return p


def read(repo, rel):
    return (repo.root / rel).read_text()


def run_rewind(args, cwd, env=None, input_text=None, timeout=60):
    full_env = dict(os.environ)
    full_env.update(env or {})
    return subprocess.run([sys.executable, "-m", "rewind_cli"] + list(args), cwd=str(cwd), env=full_env,
                          input=input_text, capture_output=True, text=True, timeout=timeout)


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """A throwaway $HOME (config files for AI tools live there)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    return home


@pytest.fixture
def stop_watchers():
    """Stop every background watcher started for registered folders during a test."""
    yield
    from rewind_cli import registry
    from rewind_cli.watch import stop_daemon

    for entry in registry.load():
        try:
            stop_daemon(registry.repo_for(entry))
        except Exception:
            pass
