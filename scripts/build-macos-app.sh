#!/bin/sh
# Build a minimal macOS app bundle, Rewind.app, whose executable runs `rewind app`.
#
#   scripts/build-macos-app.sh                # writes dist/Rewind.app
#   scripts/build-macos-app.sh ~/Applications # or somewhere else
#
# The bundle is a launcher, not a packaged Python: Rewind itself must already be
# installed (pipx install "git+https://github.com/rushi053/rewind"). The absolute path of `rewind` is baked in,
# because apps started from Finder don't see your shell's PATH.
# The icon is the prebuilt icon.icns shipped in the package (assets/icon.svg is
# the source; scripts/make-icons.py regenerates it). If it's missing, the 256px
# PNG is used instead.
#
# `rewind app install-launcher` does the same thing from Python.
set -eu

dest="${1:-dist}"
rewind_bin="${REWIND_BIN:-$(command -v rewind || true)}"
if [ -z "$rewind_bin" ]; then
  echo "build-macos-app: \`rewind\` is not on your PATH. Install it first: pipx install \"git+https://github.com/rushi053/rewind\"" >&2
  exit 1
fi
case "$rewind_bin" in
  /*) ;;
  *) rewind_bin="$(cd "$(dirname "$rewind_bin")" && pwd)/$(basename "$rewind_bin")" ;;
esac

root="$(cd "$(dirname "$0")/.." && pwd)"
static="$root/src/rewind_cli/static"
version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$root/src/rewind_cli/__init__.py")"
app="$dest/Rewind.app"

rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"

if [ -f "$static/icon.icns" ]; then
  cp "$static/icon.icns" "$app/Contents/Resources/Rewind.icns"
  icon_file="Rewind"
else
  cp "$static/icon-256.png" "$app/Contents/Resources/Rewind.png"
  icon_file="Rewind.png"
fi

cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Rewind</string>
  <key>CFBundleDisplayName</key><string>Rewind</string>
  <key>CFBundleIdentifier</key><string>dev.rewind.app</string>
  <key>CFBundleExecutable</key><string>Rewind</string>
  <key>CFBundleIconFile</key><string>$icon_file</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$version</string>
  <key>CFBundleVersion</key><string>$version</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>LSMinimumSystemVersion</key><string>10.13</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

cat > "$app/Contents/MacOS/Rewind" <<LAUNCH
#!/bin/sh
# Opens the Rewind app in your browser (starts it in the background if needed).
exec "$rewind_bin" app "\$@" >/dev/null 2>&1
LAUNCH
chmod 755 "$app/Contents/MacOS/Rewind"

if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$app/Contents/Info.plist" >/dev/null
fi
echo "Built $app (runs: $rewind_bin app)"
