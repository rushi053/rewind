# Security policy

## Reporting a vulnerability

Please report security issues privately through GitHub's "Report a vulnerability" button on the repository's Security tab, rather than in a public issue. Include steps to reproduce and the Rewind version (`rewind --version`). You should get a response within a week.

## What Rewind does and does not protect against

Rewind is a local undo history, not a security boundary.

- **The seatbelt is a heuristic.** It catches common destructive shell commands from cooperative agents. It does not stop a process that is determined to cause damage, a script the agent writes and then executes, or commands it does not recognise.
- **Snapshots are not encrypted.** Copies of every snapshotted file, including `.env` files and other secrets, are stored zlib-compressed in `.rewind/objects` (or `~/.rewind/projects/<hash>` with `--global`) with your normal file permissions. Treat the store like the project itself. `.rewind/` ships with a `.gitignore` so it is never committed.
- **The web UI** binds to `127.0.0.1` only, requires a random per-run token sent as a request header for every API call, rejects requests whose `Host` is not `127.0.0.1`/`localhost`, and sends a restrictive Content-Security-Policy. Anyone who can read the printed URL (for example another user on a shared machine looking at your terminal) can use it while it runs.
- **Hooks run as you.** Installing hooks lets the agent tool execute `rewind hook exec` on every tool call. Review `.claude/settings.json`, `.codex/hooks.json`, `.gemini/settings.json` or `.cursor/hooks.json` after installing, as you would any hook.
- **Restores stay inside the project.** Rewind refuses to write through a symlink whose target leaves the project directory.

Issues in any of the above are in scope.
