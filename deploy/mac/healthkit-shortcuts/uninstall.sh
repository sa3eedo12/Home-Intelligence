#!/usr/bin/env bash
# Remove the HealthKit Shortcuts -> Home Intelligence LaunchAgent.
# Does NOT delete the Shortcut itself or any synced HealthKit data.

set -euo pipefail

LABEL="com.home-intelligence.healthkit-shortcuts"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ -f "$PLIST" ]]; then
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed: $PLIST"
else
  echo "Nothing to remove: $PLIST does not exist."
fi

echo "(The 'HI Health Snapshot' Shortcut and your local logs were left in place.)"
echo "  Logs:     $HOME/Library/Logs/healthkit-shortcuts*.log"
echo "  Shortcut: open Shortcuts.app to remove it manually if you wish."
