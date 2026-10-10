#!/usr/bin/env bash
# End-to-end smoke test: a "disastrous agent" wrecks a project, `rewind undo --session` puts it back.
# Usage: scripts/disaster-demo.sh   (needs git and `rewind` or REWIND="python -m rewind_cli")
set -euo pipefail

REWIND=${REWIND:-rewind}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

sha() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$@"; else shasum -a 256 "$@"; fi | cut -d' ' -f1; }
mode() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"; }

fingerprint() {
  # hash of every file except the rewind store: path, mode and content (symlinks by target)
  find . -path ./.rewind -prune -o \( -type f -o -type l \) -print | LC_ALL=C sort | while read -r f; do
    if [ -L "$f" ]; then printf 'L %s -> %s\n' "$f" "$(readlink "$f")"
    else printf '%s %s %s\n' "$(mode "$f")" "$(sha "$f")" "$f"; fi
  done | sha
}

say "Setting up a project with committed, uncommitted and untracked work"
git init -q -b main
git config user.email demo@example.com && git config user.name demo
mkdir -p src docs data
printf 'def add(a, b):\n    return a + b\n' > src/math.py
printf '# Docs\n' > docs/index.md
git add -A && git commit -qm "initial commit"
printf 'def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b  # uncommitted work\n' > src/math.py
printf 'API_KEY=sk-live-very-secret\n' > data/.env          # untracked secret
head -c 4096 /dev/urandom > data/local.db                  # untracked binary "database"
printf '#!/bin/sh\necho deploy\n' > deploy.sh && chmod 755 deploy.sh
ln -s src/math.py link-to-math.py

say "rewind init"
$REWIND init --no-hooks
BEFORE=$(fingerprint)
echo "fingerprint before the agent: $BEFORE"

cat > evil_agent.sh <<'AGENT'
set -x
sleep 1.5                                  # let rewind's watcher settle
printf 'garbage\n' > src/math.py           # clobber a file with uncommitted work
rm -rf docs                                # delete a folder
rm -rf data                                # rm -rf a dir holding an untracked .env and a local db
printf 'oops\n' > new-file.txt             # create stray files
chmod 600 deploy.sh
git reset --hard -q                        # throw away whatever uncommitted work is left
rm -f .git/index .git/refs/heads/main      # and corrupt the git repo for good measure
sleep 1
AGENT
mv evil_agent.sh /tmp/evil_agent_$$.sh

say "Running the disastrous agent under 'rewind run'"
$REWIND run --name evil-agent -- bash /tmp/evil_agent_$$.sh || true
rm -f /tmp/evil_agent_$$.sh

say "Damage report"
ls -la; echo; git status 2>&1 | head -3 || true
DAMAGED=$(fingerprint)
echo "fingerprint after agent: $DAMAGED"
[ "$DAMAGED" != "$BEFORE" ] || { echo "agent did no damage?"; exit 1; }

say "rewind log"
$REWIND log

say "rewind undo --session -y"
$REWIND undo --session -y

AFTER=$(fingerprint)
echo "fingerprint after undo: $AFTER"
say "Verification"
cat data/.env
git log --oneline
git diff --stat
if [ "$AFTER" = "$BEFORE" ]; then
  printf '\n\033[1;32mPASS\033[0m every file (including .git, data/.env, data/local.db, modes and symlinks) is byte-identical to before the agent ran.\n'
else
  printf '\n\033[1;31mFAIL\033[0m fingerprints differ\n'; exit 1
fi
