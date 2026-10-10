# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `rewind oops`: preview, then undo the latest agent session. `rewind redo` puts that session's result back. `rewind undo --session` records the same redo step.
- The seatbelt looks inside `python -c` / `python3 -c`, `perl -e`, `ruby -e` and `node -e` one-liners (`shutil.rmtree`, `os.remove` on a broad path, `perl` `unlink`/`remove_tree`, `FileUtils.rm_rf`, `fs.rmSync` with `recursive`) and treats `busybox` / `toybox` as wrappers. `rm -rf node_modules` is still allowed. It is still a heuristic.
- GitHub Actions workflow `.github/workflows/publish.yml` publishes `rewind-cli` to PyPI with trusted publishing when a `v*` tag is pushed. It does nothing until that tag exists and the PyPI project trusts this repository.

### Changed
- New projects keep their store in `~/.rewind/projects/<hash>`. `rewind init --local` (and the app's "Inside the folder" option) opts into `.rewind/` in the project. An existing in-project store is kept, not migrated. `rewind init` prints that `.env` snapshots are plaintext local copies.
- A store lock is taken from a dead pid only. A live process keeps the lock no matter how old the lock file is.
- `rewind app` no longer asks DNS for the name of `127.0.0.1` (that lookup could hang on macOS before the server wrote `app.json`). If the app still fails to start, the error includes the tail of `app.log`, and CI prints that file on failure.
- Install from git (`pipx install git+https://github.com/rushi053/rewind`) until `rewind-cli` is published. `install.sh` and `install.ps1` do the same, so they do not ask PyPI for a package that 404s.
- Refreshed the app's look: bundled Geist and Geist Mono (SIL OFL 1.1, in `static/fonts/`, served locally with long-lived caching, so the app still works offline), a tuned neutral palette with one accent and a first-class dark theme, consistent spacing, radii, borders and soft shadows, refined 1.5px icons, status pills, empty states and focus rings.
- Calm motion throughout: page and tab transitions, staggered lists, a sliding sidebar and tab indicator, springy switches and checkboxes, a modal that scales in over a blurred backdrop, skeleton loading states, toasts with icons, slider feedback, an animated theme switch, and a check-mark confirmation after Undo, Redo and Go back. All of it is turned off under "reduce motion".
- The timeline view (`rewind ui`, and the Timeline tab) matches the new design.
- Accessibility: switches report their state (`role="switch"`), dialogs keep focus inside and return it on close, and status text meets AA contrast in both themes.
- `scripts/screenshots.py` now writes every screen in light and dark; new `scripts/record-motion.py` records `docs/app-motion.mp4`.

## [0.2.0] - 2026-10-10

The Rewind app, for people who never open a terminal, and integration with AI tools through MCP.

### Added
- `rewind app`: a local control center in the browser (stdlib `http.server` on 127.0.0.1, token-protected). One background server per user, reused when it's already running.
  - Home screen of protected folders with status (Protected / Paused / Starting), last save and space used, and a switch that starts or stops a background watcher per folder. The server restarts watchers that die.
  - "Protect a folder" with a server-side folder browser, projects found on the computer, and common places (Desktop, Documents, `~/code`, ...). Refuses system folders, your whole home folder, and nested folders.
  - Per folder: "Undo last change" and "Redo", "Go back in time" (5 minutes, 1 hour, this morning, yesterday, or a slider over every saved moment) with a plain-language preview before confirming, "Undo this AI session", a "Deleted files" view with one-click bring-back, the existing timeline/diff view embedded, and per-folder settings.
  - "Connect AI tools": detects Claude Code, Codex CLI, Gemini CLI, Cursor, Claude Desktop, Windsurf and VS Code; connects hooks and/or MCP per folder or for all folders; shows the exact file and a diff first and backs the file up to `~/.rewind/backups/`.
  - Settings: safety mode (block / save a copy then allow / warn / off) explained in plain words, history retention, storage used with Clean up, notifications, start at login.
  - Activity feed (`~/.rewind/activity.jsonl`) and first-run onboarding (pick a folder, connect your AI tools, done).
- Global folder registry (`~/.rewind/registry.json`); `rewind init` registers folders and `rewind watch --daemon`/`stop` keep it in sync.
- Desktop notification when the seatbelt blocks a command (best effort: `osascript` on macOS, `notify-send` on Linux), and the block is recorded in the activity feed.
- Launchers: `rewind app install-launcher` (macOS `~/Applications/Rewind.app`, Linux `.desktop` entry, Windows `.vbs`/`.bat` plus shortcuts), `scripts/build-macos-app.sh`, generic launchers in `scripts/launchers/`, and an app icon (`.icns`, `.ico`, `.png`, built from `assets/icon.svg`).
- `rewind app --autostart on|off`: LaunchAgent (macOS), systemd user unit or XDG autostart (Linux), Startup folder entry (Windows).
- `install.sh` now adds the launcher; new `install.ps1` for Windows.
- `rewind mcp`: an MCP server over stdio (JSON-RPC 2.0, protocol `2025-06-18`, negotiates `2025-03-26` and `2024-11-05`) with the tools `checkpoint`, `list_history`, `show_changes`, `undo_last`, `restore`, `recover_deleted` and `check_command`. Tools that change files only preview unless called with `confirm: true`.
- `rewind mcp install|uninstall|config <client>` for Claude Desktop, Cursor, Windsurf, VS Code and Claude Code, merging into existing configs (JSON with comments is read) with a backup first.
- Claude Code slash commands `/rewind-undo`, `/rewind-log`, `/rewind-checkpoint`, installed by `rewind hook install claude`.
- `rewind hook install <agent> --global` for user-level hooks in `~/.claude`, `~/.codex`, `~/.gemini` and `~/.cursor`.

### Changed
- The `rewind ui` page can be embedded per folder (`/ui?f=<id>&embed=1`) and follows the app's theme.

### Not verified yet
- The macOS app bundle, LaunchAgent, Windows launchers and Startup entry have only been generated and inspected on Linux, not run on macOS or Windows.
- The MCP configs and slash commands follow each tool's documentation and are covered by tests, but haven't been tried inside the tools themselves.

## [0.1.0] - 2026-10-10

First release.

### Added
- Content-addressed snapshot store (sha256, zlib) with an incremental stat index; snapshots include untracked files and the `.git` directory; `.rewindignore` with defaults.
- `rewind init`, `snap`, `watch` (foreground and daemon), `run -- <cmd>` sessions.
- `rewind undo`, `redo`, `undo --session`, `restore`, `back <time>`, `recover <path|glob>`, all with preview, confirmation, `--dry-run`, and a safety snapshot before every change.
- `rewind log`, `show`, `diff`, `status`, `doctor`, `gc` (retention policy), `config`.
- The seatbelt (`rewind guard`): detection of destructive shell commands with block/snapshot/warn modes, allowlist and per-rule disabling.
- Hook installers for Claude Code, Codex CLI, Gemini CLI and Cursor.
- `rewind ui`: local web UI with timeline, file list, unified/split diffs, restore and keyboard shortcuts.
- `install.sh` one-line installer.
