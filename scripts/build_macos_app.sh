#!/usr/bin/env bash
# Build "Security Patch Monitor.app" that runs this checkout from its virtual environment.
#
#   scripts/build_macos_app.sh [destination-dir]    (default: ~/Applications)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-$HOME/Applications}"
APP="$DEST/Security Patch Monitor.app"
VENV="$ROOT/.venv"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml")"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "This script builds a macOS app bundle and must run on macOS." >&2
  exit 1
fi

if [[ ! -x "$VENV/bin/python" ]]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -e "$ROOT[desktop]"  # editable: the app runs this checkout

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$ROOT/packaging/macos/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/MacOS/SecurityPatchMonitor" <<EOF
#!/usr/bin/env bash
exec "$VENV/bin/security-patch-monitor" "\$@"
EOF
chmod +x "$APP/Contents/MacOS/SecurityPatchMonitor"

cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Security Patch Monitor</string>
  <key>CFBundleDisplayName</key><string>Security Patch Monitor</string>
  <key>CFBundleIdentifier</key><string>local.security-patch-monitor</string>
  <key>CFBundleExecutable</key><string>SecurityPatchMonitor</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
EOF

touch "$APP"  # make Finder pick up the icon
echo "Built $APP (version $VERSION)"
