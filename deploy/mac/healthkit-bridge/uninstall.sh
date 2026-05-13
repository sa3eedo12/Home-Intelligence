#!/usr/bin/env bash
# Stop and remove the Health Auto Export bridge LaunchAgent.

set -euo pipefail

LABEL="com.home-intelligence.healthkit-bridge"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "Removed $LABEL"
echo "Logs left at $HOME/Library/Logs/healthkit-bridge*.log (delete manually if you want)"
