#!/bin/bash
# make_app.sh — build Ivy.app for macOS. Absolute paths, no surprises.
set -e

# ============================================================
# PATHS (resolved at build time, baked into the launcher)
# ============================================================

PROJECT_DIR="/Users/romeu/IVY-1"
VENV_PY="$PROJECT_DIR/.venv/bin/python3"
ENTRY="$PROJECT_DIR/ivy_gui.py"
APP_DIR="$HOME/Applications/Ivy.app"
LOG_FILE="$HOME/Library/Logs/Ivy.log"

# Icon: try the two known locations, first match wins
ICON_CANDIDATES=(
    "$PROJECT_DIR/ivy/ui/AppIcon.icns"
    "$PROJECT_DIR/ivy/assets/Ivy.icns"
)

# ============================================================
# SANITY CHECKS
# ============================================================

echo "Building Ivy.app"
echo "  Project: $PROJECT_DIR"
echo "  Entry:   $ENTRY"
echo "  Python:  $VENV_PY"
echo "  Output:  $APP_DIR"
echo ""

if [ ! -d "$PROJECT_DIR" ]; then
    echo "❌ Project directory not found: $PROJECT_DIR"
    exit 1
fi

if [ ! -x "$VENV_PY" ]; then
    echo "❌ venv python not executable: $VENV_PY"
    echo "   Rebuild the venv: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

if [ ! -f "$ENTRY" ]; then
    echo "❌ Entry script not found: $ENTRY"
    exit 1
fi

# ============================================================
# CLEAN OLD BUNDLES
# ============================================================

for old in "MAE-2.app" "Mae-2.app" "Mae.app"; do
    if [ -d "$HOME/Applications/$old" ]; then
        rm -rf "$HOME/Applications/$old"
        echo "  • removed old $old"
    fi
done

rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS"
mkdir -p "$APP_DIR/Contents/Resources"

# ============================================================
# LAUNCHER (absolute paths — resolved now, not at runtime)
# ============================================================

cat > "$APP_DIR/Contents/MacOS/Ivy" <<LAUNCHER
#!/bin/bash
LOG_FILE="$LOG_FILE"

mkdir -p "\$(dirname "\$LOG_FILE")"
cd "$PROJECT_DIR" || exit 1

# rotate log at 2 MB
if [ -f "\$LOG_FILE" ] && [ "\$(stat -f%z "\$LOG_FILE")" -gt 2097152 ]; then
    mv "\$LOG_FILE" "\$LOG_FILE.old"
fi

echo "" >> "\$LOG_FILE"
echo "=== Ivy launch: \$(date) ===" >> "\$LOG_FILE"

# Run the GUI directly with the venv python — no activate needed
exec "$VENV_PY" "$ENTRY" >> "\$LOG_FILE" 2>&1
LAUNCHER

chmod +x "$APP_DIR/Contents/MacOS/Ivy"

# ============================================================
# ICON
# ============================================================

ICON_SRC=""
for candidate in "${ICON_CANDIDATES[@]}"; do
    if [ -f "$candidate" ]; then
        ICON_SRC="$candidate"
        break
    fi
done

if [ -n "$ICON_SRC" ]; then
    cp "$ICON_SRC" "$APP_DIR/Contents/Resources/AppIcon.icns"
    echo "  • icon installed from $ICON_SRC"
else
    echo "  ⚠️  no icon found — building without custom icon"
fi

# ============================================================
# INFO.PLIST
# ============================================================

cat > "$APP_DIR/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>Ivy</string>
    <key>CFBundleDisplayName</key>
    <string>Ivy</string>
    <key>CFBundleIdentifier</key>
    <string>local.ivy.assistant</string>
    <key>CFBundleExecutable</key>
    <string>Ivy</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>3.0.0</string>
    <key>CFBundleVersion</key>
    <string>300</string>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSMicrophoneUsageDescription</key>
    <string>Ivy needs the microphone to hear "OK Ivy" and your voice commands.</string>
    <key>NSAppleEventsUsageDescription</key>
    <string>Ivy uses AppleScript to control apps on your behalf.</string>
</dict>
</plist>
PLIST

# Register icon filename if we installed one
if [ -n "$ICON_SRC" ]; then
    PLIST_FILE="$APP_DIR/Contents/Info.plist"
    /usr/libexec/PlistBuddy -c "Delete :CFBundleIconFile" "$PLIST_FILE" 2>/dev/null || true
    /usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string AppIcon" "$PLIST_FILE"
fi

# ============================================================
# SIGN + REGISTER
# ============================================================

codesign --force --deep --sign - "$APP_DIR" 2>/dev/null || \
    echo "  ⚠️  ad-hoc signing failed — mic permissions may need re-grant"

/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -f "$APP_DIR" >/dev/null 2>&1 || true

# ============================================================
# DONE
# ============================================================

echo ""
echo "✅ Built: $APP_DIR"
echo ""
echo "Test it now:"
echo "  $APP_DIR/Contents/MacOS/Ivy"
echo ""
echo "Then double-click from Finder or Spotlight."
echo ""
echo "If mic doesn't work: System Settings → Privacy & Security → Microphone"
echo "  → drag Ivy.app from Finder into the list → toggle ON"
echo ""
echo "Logs: $LOG_FILE"