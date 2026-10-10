# Contributing

Thanks for helping. Rewind is small on purpose: one Python package, standard library only, no runtime dependencies. Please keep it that way.

## Setup

```sh
git clone https://github.com/rushi053/rewind && cd rewind
python -m venv .venv && . .venv/bin/activate
pip install -e . pytest
pytest
```

The suite runs in about 20 seconds. `scripts/disaster-demo.sh` is the end-to-end check CI also runs: it builds a git project, lets a script wreck it under `rewind run`, and verifies `rewind undo --session` restores every file byte-for-byte.

For the app there are three dev-only scripts that need Playwright (`pip install playwright && playwright install chromium`):

- `scripts/ui-smoke.py` clicks through every app flow in headless Chromium and checks the files on disk after each step. Run it after changing `app.html`.
- `scripts/screenshots.py` regenerates the `docs/app-*.png` screenshots from a demo home built by `scripts/make-app-demo.py` (you can also point `HOME`/`REWIND_HOME` at that demo and run `rewind app` to try the app safely).
- `scripts/make-icons.py` renders `assets/icon.svg` into the shipped `.icns`, `.ico` and `.png` files.

## Where things live

| file | what |
|---|---|
| `src/rewind_cli/store.py` | object store, snapshots, sessions, locking |
| `src/rewind_cli/scan.py` | tree walk and the stat index |
| `src/rewind_cli/ignore.py` | gitignore-style matching and the default `.rewindignore` |
| `src/rewind_cli/restore.py` | restore, undo/redo, session undo, back, recover |
| `src/rewind_cli/guard.py` | the seatbelt rules |
| `src/rewind_cli/hooks.py` | hook install/uninstall and `rewind hook exec` |
| `src/rewind_cli/watch.py` | polling watcher and daemon |
| `src/rewind_cli/ui.py`, `static/ui.html` | technical web UI (one self-contained HTML file), also embedded in the app |
| `src/rewind_cli/app.py`, `static/app.html` | the Rewind app: server, API, lifecycle, supervisor; one self-contained HTML file |
| `src/rewind_cli/registry.py` | global folder list, app settings, activity feed |
| `src/rewind_cli/plain.py` | plain-language descriptions of snapshots and restore plans |
| `src/rewind_cli/integrations.py` | hook and MCP config writers for each AI tool, with backups |
| `src/rewind_cli/mcp.py` | the MCP server (stdio JSON-RPC) |
| `src/rewind_cli/launchers.py` | app launchers and start-at-login entries per OS |
| `src/rewind_cli/notify.py` | desktop notifications |
| `src/rewind_cli/cli.py` | argument parsing and output |

## Guidelines

- **No new runtime dependencies.** Dev-only tools are fine.
- **Python 3.9+.** No `match`, no `X | Y` type unions at runtime.
- **Keep startup fast.** Import heavy modules inside the command function that needs them.
- **User errors are not tracebacks.** Raise `RewindError` with a message that says what to do.
- **Anything that changes the working tree takes a safety snapshot first** (go through `restore.execute`).
- **Hooks must fail open.** A bug in Rewind must never block an agent.
- **Tests with every change.** Seatbelt changes need both a true-positive and a false-positive case in `tests/test_guard.py`.

## Seatbelt rules

New rules should target commands that destroy data that is hard to get back, and should not fire on everyday commands (`rm -rf node_modules`, `git reset HEAD~1`, `git push --force-with-lease origin my-branch`). When in doubt, prefer a false negative: Rewind snapshots before every command anyway.

## Agent integrations

If you can verify a hook integration against a real agent session, a PR that documents the exact version you tested is very welcome. Please keep the README integration table honest about what is verified.

## Releases

Maintainers: bump `__version__` in `src/rewind_cli/__init__.py`, update `CHANGELOG.md`, tag `vX.Y.Z`, then `python -m build && twine upload dist/*`.
