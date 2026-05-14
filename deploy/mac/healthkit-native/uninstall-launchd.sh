#!/usr/bin/env bash
# Remove the HealthKit-native LaunchAgent. Does NOT delete the app bundle
# itself (use Finder if you want to remove it) or any HealthKit data.

set -euo pipefail

LABEL="com.home-intelligence.healthkit-native"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ -f "$PLIST" ]]; then
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed: $PLIST"
else
  echo "Nothing to remove: $PLIST does not exist."
fi

echo "(The HomeIntelligenceHealth.app bundle and your local logs were left in place.)"
echo "  Logs:  $HOME/Library/Logs/HomeIntelligenceHealth*.log"
