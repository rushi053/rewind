#!/usr/bin/env bash
# Builds a small demo project with real Rewind history (used for the README screenshots).
# Usage: scripts/make-demo-project.sh <dir>
set -euo pipefail
DIR=${1:?usage: make-demo-project.sh <dir>}
REWIND=${REWIND:-rewind}
rm -rf "$DIR"; mkdir -p "$DIR"; cd "$DIR"
git init -q -b main; git config user.email demo@example.com; git config user.name demo
mkdir -p src tests
cat > src/server.py <<'PY'
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8080


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")


def main():
    HTTPServer(("", PORT), Handler).serve_forever()
PY
cat > src/todo.py <<'PY'
def add(items, text):
    items.append({"text": text, "done": False})
    return items


def complete(items, index):
    items[index]["done"] = True
    return items
PY
printf 'from src.todo import add\n\n\ndef test_add():\n    assert add([], "x") == [{"text": "x", "done": False}]\n' > tests/test_todo.py
printf '# todo-api\n\nA tiny todo service.\n' > README.md
git add -A && git commit -qm "initial commit"
printf 'DATABASE_URL=postgres://localhost/todo\nSTRIPE_KEY=sk_test_123\n' > .env

$REWIND init --no-hooks >/dev/null
$REWIND hook install claude >/dev/null

hook() { # agent event tool json-input
  printf '{"session_id":"demo-claude-1","cwd":"%s","hook_event_name":"%s","tool_name":"%s","tool_input":%s}' "$PWD" "$2" "$3" "$4" \
    | $REWIND hook exec "$1" "$2" >/dev/null 2>&1 || true
}

printf '\n# TODO: persistence\n' >> README.md
$REWIND snap -m "before letting the agent loose" >/dev/null

# --- a Claude Code session, driven through the real hook entry point
hook claude PreToolUse Edit '{"file_path":"src/todo.py"}'
cat > src/todo.py <<'PY'
import json
from pathlib import Path

DB = Path("todos.json")


def load():
    return json.loads(DB.read_text()) if DB.exists() else []


def save(items):
    DB.write_text(json.dumps(items, indent=2))


def add(items, text):
    items.append({"text": text, "done": False})
    save(items)
    return items


def complete(items, index):
    items[index]["done"] = True
    save(items)
    return items
PY
hook claude PostToolUse Edit '{"file_path":"src/todo.py"}'
hook claude PreToolUse Write '{"file_path":"src/storage.py"}'
printf 'class Storage:\n    """Placeholder for a real database."""\n' > src/storage.py
hook claude PostToolUse Write '{"file_path":"src/storage.py"}'
hook claude PreToolUse Bash '{"command":"pytest -q"}'
hook claude PostToolUse Bash '{"command":"pytest -q"}'
hook claude PreToolUse Bash '{"command":"git reset --hard HEAD"}'   # blocked by the seatbelt
hook claude PreToolUse Bash '{"command":"rm -rf tests && pytest -q"}'  # "fixes" the failing tests
rm -rf tests
hook claude PostToolUse Bash '{"command":"rm -rf tests && pytest -q"}'

# --- a wrapped session (any CLI agent)
cat > /tmp/fake-agent.sh <<'SH'
sleep 1.2
printf 'PORT = 9000\n' > config.py
rm -f .env
sleep 1
SH
$REWIND run --name aider -- bash /tmp/fake-agent.sh >/dev/null 2>&1
rm -f /tmp/fake-agent.sh
echo "demo project ready in $DIR"
