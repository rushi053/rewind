# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
