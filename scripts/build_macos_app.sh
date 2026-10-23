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
# Tells the app where its bundle (and the notifier helper inside it) lives.
export SPM_APP_BUNDLE="$APP"
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

# Native notifier helper: notifications show the app's own name and icon (see packaging/macos/notifier).
HELPER="$APP/Contents/Helpers/Security Patch Monitor Notifier.app"
if command -v swiftc >/dev/null 2>&1; then
  mkdir -p "$HELPER/Contents/MacOS" "$HELPER/Contents/Resources"
  SWIFT_FLAGS=()
  # Some Command Line Tools releases ship two module maps that both define SwiftBridging, which breaks every
  # build. Hide the duplicate from the compiler with a VFS overlay instead of modifying the installation.
  CLT_SWIFT="$(xcode-select -p)/usr/include/swift"
  if [[ -f "$CLT_SWIFT/module.modulemap" && -f "$CLT_SWIFT/bridging.modulemap" ]]; then
    OVERLAY_DIR="$(mktemp -d)"
    trap 'rm -rf "$OVERLAY_DIR"' EXIT
    : > "$OVERLAY_DIR/empty.modulemap"
    printf '{"version": 0, "roots": [{"type": "file", "name": "%s", "external-contents": "%s"}]}\n' \
      "$CLT_SWIFT/module.modulemap" "$OVERLAY_DIR/empty.modulemap" > "$OVERLAY_DIR/overlay.yaml"
    SWIFT_FLAGS+=(-vfsoverlay "$OVERLAY_DIR/overlay.yaml" -Xcc -ivfsoverlay -Xcc "$OVERLAY_DIR/overlay.yaml")
  fi
  swiftc -O -swift-version 5 "${SWIFT_FLAGS[@]}" -o "$HELPER/Contents/MacOS/notifier" \
    "$ROOT/packaging/macos/notifier/main.swift" -framework AppKit -framework UserNotifications
  cp "$ROOT/packaging/macos/AppIcon.icns" "$HELPER/Contents/Resources/AppIcon.icns"
  cat > "$HELPER/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Security Patch Monitor</string>
  <key>CFBundleDisplayName</key><string>Security Patch Monitor</string>
  <key>CFBundleIdentifier</key><string>local.security-patch-monitor.notifier</string>
  <key>CFBundleExecutable</key><string>notifier</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSUIElement</key><true/>
</dict></plist>
EOF
  codesign --force --sign - "$HELPER" >/dev/null  # ad-hoc signature; macOS requires signed apps for notifications
  /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$HELPER"
else
  echo "swiftc not found: notifications fall back to AppleScript (install the Xcode Command Line Tools)." >&2
fi

touch "$APP"  # make Finder pick up the icon
echo "Built $APP (version $VERSION)"
