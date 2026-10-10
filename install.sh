#!/bin/sh
# Rewind installer: installs the `rewind` CLI with uv, pipx, or pip (in that order).
#
#   curl -fsSL https://raw.githubusercontent.com/rushi053/rewind/main/install.sh | sh
#
# (Yes, Rewind's own seatbelt flags `curl | sh`. Read this script first; it is short.)
#
# Environment:
#   REWIND_VERSION   install a specific version, e.g. 0.1.0
#   REWIND_SOURCE    install from somewhere else, e.g. git+https://github.com/rushi053/rewind
set -eu

pkg="rewind-cli"
if [ -n "${REWIND_SOURCE:-}" ]; then
  pkg="$REWIND_SOURCE"
elif [ -n "${REWIND_VERSION:-}" ]; then
  pkg="rewind-cli==$REWIND_VERSION"
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

if have rewind; then
  say ""
  say "Installed: $(rewind --version)"
  say "Next:  cd your-project && rewind init"
else
  say ""
  say "Installed, but \`rewind\` is not on your PATH yet."
  say "uv/pipx users: run \`uv tool update-shell\` or \`pipx ensurepath\`, then open a new terminal."
fi
