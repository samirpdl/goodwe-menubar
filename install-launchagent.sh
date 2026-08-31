#!/usr/bin/env bash
# Install a LaunchAgent so the menubar app starts at login and restarts if it
# crashes (but stays quit if you choose Quit). Run once:  ./install-launchagent.sh
# Remove with:  launchctl unload ~/Library/LaunchAgents/com.local.goodwe-menubar.plist
set -euo pipefail
cd "$(dirname "$0")"
PROJECT_DIR="$(pwd -P)"
PY="$PROJECT_DIR/.venv/bin/python"
LABEL="com.local.goodwe-menubar"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/goodwe-menubar.log"

if [[ ! -x "$PY" ]]; then
  echo "No venv at $PY — create it and install deps first (see README)." >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

# Optional Tesla solar-charging support: a launchd job inherits no shell
# profile, so bake these in if they're set when you run this script.
TESLA_ENV=""
if [ -n "$TESLA_VIN" ] && [ -n "$TESLA_KEY_FILE" ]; then
  TESLA_ENV="  <key>EnvironmentVariables</key>
  <dict>
    <key>TESLA_VIN</key>      <string>$TESLA_VIN</string>
    <key>TESLA_KEY_FILE</key> <string>$TESLA_KEY_FILE</string>
    <key>TESLA_CACHE_FILE</key><string>${TESLA_CACHE_FILE:-$HOME/.tesla/cache.json}</string>
    <key>PATH</key>           <string>$HOME/go/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>"
  echo "Tesla solar charging: enabled for VIN $TESLA_VIN"
fi

cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>            <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>-m</string>
    <string>goodwe_menubar</string>
  </array>
  <key>WorkingDirectory</key> <string>$PROJECT_DIR</string>
  <key>RunAtLoad</key>        <true/>
  <key>KeepAlive</key>        <dict><key>SuccessfulExit</key><false/></dict>
  <key>ProcessType</key>      <string>Interactive</string>
  <key>StandardOutPath</key>  <string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
$TESLA_ENV
</dict>
</plist>
PL

DOMAIN="gui/$(id -u)"
# bootout is asynchronous, so bootstrapping straight afterwards races the
# teardown and fails with "Bootstrap failed: 5: Input/output error". Wait for
# the label to actually disappear, and if it's still registered just restart it
# in place — kickstart picks up the rewritten plist either way.
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
for _ in 1 2 3 4 5 6 7 8 9 10; do
  launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
  sleep 1
done
# bootstrap frequently reports "Bootstrap failed: 5: Input/output error" while
# having loaded the job perfectly well, so its exit code is worthless here.
# Ask launchd what actually happened instead.
launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null || true
launchctl kickstart -k "$DOMAIN/$LABEL" 2>/dev/null || true
if ! launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  echo "error: $LABEL did not register with launchd." >&2
  echo "Check: plutil -lint \"$PLIST\"  and  ls -l \"$PY\"" >&2
  exit 1
fi

echo "Installed and bootstrapped into $DOMAIN: $PLIST"
echo "Logs: $LOG"
echo "It now starts automatically at login. (A clean Quit stays quit; crashes auto-restart.)"
