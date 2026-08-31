#!/usr/bin/env bash
# Build a double-clickable "GoodWe Solar.app" that STARTS the menubar app.
#
# It does NOT run the menubar itself (a wrapper that exec's Python loses
# menubar access when launched via Finder). Instead its tiny native launcher
# just asks launchd to start the LaunchAgent-managed instance — which runs
# Python directly and therefore shows its menubar icon correctly.
#
# Prereq: run ./install-launchagent.sh first (it registers the LaunchAgent).
set -euo pipefail
cd "$(dirname "$0")"
UID_NUM="$(id -u)"
LABEL="com.local.goodwe-menubar"
APP="GoodWe Solar.app"
EXE="GoodWeSolar"

if ! command -v clang >/dev/null 2>&1; then
  echo "clang not found — install Xcode Command Line Tools: xcode-select --install" >&2
  exit 1
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>               <string>GoodWe Solar</string>
  <key>CFBundleDisplayName</key>        <string>GoodWe Solar</string>
  <key>CFBundleIdentifier</key>         <string>com.local.goodwe-solar-launcher</string>
  <key>CFBundleVersion</key>            <string>0.1.0</string>
  <key>CFBundleShortVersionString</key> <string>0.1.0</string>
  <key>CFBundlePackageType</key>        <string>APPL</string>
  <key>CFBundleExecutable</key>         <string>$EXE</string>
  <key>LSUIElement</key>                <true/>
  <key>LSMinimumSystemVersion</key>     <string>12.0</string>
</dict>
</plist>
PLIST

# Native launcher: ensure the LaunchAgent-managed instance is running.
TMP_C="${TMPDIR:-/tmp}/gw_launcher_$$.c"
cat > "$TMP_C" <<LAUNCHER
#include <unistd.h>
int main(void) {
    /* Start the menubar app via launchd (it shows its icon correctly because
       launchd runs Python directly). No-op if it is already running. */
    execl("/bin/launchctl", "launchctl", "kickstart",
          "gui/$UID_NUM/$LABEL", (char *)0);
    _exit(127); /* exec failed */
}
LAUNCHER

clang -arch arm64 -O2 -o "$APP/Contents/MacOS/$EXE" "$TMP_C"
rm -f "$TMP_C"
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true

echo "Built: $(pwd -P)/$APP"
file "$APP/Contents/MacOS/$EXE"
