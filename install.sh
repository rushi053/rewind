#!/bin/sh
# Rewind installer: installs the `rewind` CLI with uv, pipx, or pip (in that order),
# then adds a Rewind launcher (Applications on macOS, the app menu on Linux).
#
# Supported install (rewind-cli is not on PyPI yet):
#   pipx install git+https://github.com/rushi053/rewind
#
# This script does the same install, then adds a launcher. Do not pipe it from
# curl until you have read it: Rewind's seatbelt flags `curl | sh`, and the
# copy of this file on GitHub main is what that URL would run.
#
# Environment:
#   REWIND_VERSION   install a tag, e.g. 0.2.0 (git tag v0.2.0; none exist yet)
#   REWIND_SOURCE    override the package, e.g. rewind-cli once it is published
#   REWIND_NO_LAUNCHER=1   don't create the Rewind launcher
#   REWIND_AUTOSTART=1     also start Rewind at login (keeps folders protected after a reboot)
set -eu

# rewind-cli is not on PyPI yet. Install from git so this script cannot 404.
# After the owner publishes, REWIND_SOURCE=rewind-cli selects the package.
pkg="${REWIND_SOURCE:-git+https://github.com/rushi053/rewind}"
if [ -z "${REWIND_SOURCE:-}" ] && [ -n "${REWIND_VERSION:-}" ]; then
  pkg="git+https://github.com/rushi053/rewind@v${REWIND_VERSION#v}"
fi

say() { printf '%s\n' "$*"; }
die() { printf 'rewind install: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

if have uv; then
  say "Installing $pkg with uv..."
  uv tool install --force "$pkg"
elif have pipx; then
  say "Installing $pkg with pipx..."
  pipx install --force "$pkg"
else
  py=""
  for candidate in python3 python; do
    if have "$candidate" && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      py="$candidate"; break
    fi
  done
  [ -n "$py" ] || die "Python 3.9+ is required (or install uv: https://docs.astral.sh/uv/)"
  say "Installing $pkg with $py -m pip --user (consider uv or pipx for isolated installs)..."
  "$py" -m pip install --user --upgrade "$pkg" || die "pip install failed"
fi

rewind_cmd=""
if have rewind; then
  rewind_cmd="rewind"
else
  for candidate in "$HOME/.local/bin/rewind" "$HOME/Library/Python/"*/bin/rewind; do
    if [ -x "$candidate" ]; then rewind_cmd="$candidate"; break; fi
  done
fi

if [ -z "$rewind_cmd" ]; then
  say ""
  say "Installed, but \`rewind\` is not on your PATH yet."
  say "uv/pipx users: run \`uv tool update-shell\` or \`pipx ensurepath\`, then open a new terminal."
  exit 0
fi

say ""
say "Installed: $("$rewind_cmd" --version)"
if [ -z "${REWIND_NO_LAUNCHER:-}" ]; then
  "$rewind_cmd" app install-launcher || say "(could not create the launcher; run \`rewind app\` instead)"
fi
if [ -n "${REWIND_AUTOSTART:-}" ]; then
  "$rewind_cmd" app --autostart on || true
fi
say ""
say "Open Rewind from your apps, or run:  rewind app"
say "Prefer the terminal?                 cd your-project && rewind init"
if ! have rewind; then
  say ""
  say "Note: \`rewind\` is not on your PATH yet. Run \`uv tool update-shell\` or \`pipx ensurepath\`, then open a new terminal."
fi
