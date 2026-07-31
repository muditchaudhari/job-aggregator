#!/usr/bin/env bash
# Register the stack with launchd so it starts at login and restarts if it dies.
#
# macOS user agents run as you, with your permissions, and need no sudo. The
# job runs `stack.sh start`, which is idempotent — launchd re-running it when
# the machine wakes is harmless.
#
#   bash scripts/install-autostart.sh          install and load
#   bash scripts/install-autostart.sh remove   unload and delete

set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT="$(pwd)"
LABEL="com.jobaggregator.stack"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ "${1:-install}" = "remove" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "removed $LABEL"
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT/.run/logs"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$PROJECT/scripts/stack.sh</string>
    <string>start</string>
  </array>
  <key>WorkingDirectory</key><string>$PROJECT</string>
  <key>RunAtLoad</key><true/>
  <!-- Homebrew's bin is absent from a launchd job's default PATH, and
       Postgres and Redis both live there. -->
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StandardOutPath</key><string>$PROJECT/.run/logs/launchd.log</string>
  <key>StandardErrorPath</key><string>$PROJECT/.run/logs/launchd.log</string>
  <!-- Not KeepAlive: stack.sh spawns three children and exits, so launchd
       would read that exit as a crash and relaunch it in a tight loop. -->
</dict>
</plist>
PLIST_EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "installed $LABEL"
echo "  starts at login, logs to .run/logs/"
echo "  remove with: bash scripts/install-autostart.sh remove"
