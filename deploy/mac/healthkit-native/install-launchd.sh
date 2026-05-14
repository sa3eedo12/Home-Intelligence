#!/usr/bin/env bash
# Install the HealthKit-native app as a LaunchAgent. Idempotent: re-running
# updates the agent in place. Run AFTER you've built and signed the app
# in Xcode (see README.md), and AFTER you've launched it once manually so
# the HealthKit permission prompt has been answered.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
TEMPLATE="$SCRIPT_DIR/com.home-intelligence.healthkit-native.plist.template"

LABEL="com.home-intelligence.healthkit-native"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

prompt() {
  local var="$1" prompt_text="$2" default="${3:-}" reply
  if [[ -n "${!var:-}" ]]; then
    echo "Using $var from environment"
    return
  fi
  if [[ -n "$default" ]]; then
    read -r -p "$prompt_text [$default]: " reply
    reply="${reply:-$default}"
  else
    read -r -p "$prompt_text: " reply
  fi
  printf -v "$var" '%s' "$reply"
}

prompt APP_PATH         "Path to HomeIntelligenceHealth.app (built by Xcode)" \
                        "$HOME/Applications/HomeIntelligenceHealth.app"
prompt ORCHESTRATOR_URL "TrueNAS orchestrator URL" "http://truenas.local:8080"
prompt HEALTHKIT_TOKEN  "HEALTHKIT_WEBHOOK_TOKEN value (the same one set on TrueNAS)"
prompt WINDOW_MINUTES   "How many minutes back each run should look" "60"
prompt INTERVAL_MINUTES "Run every N minutes" "15"
prompt MEMBER_ID        "Optional household_members.id to attribute uploads to (blank to auto-resolve)" ""

# Sanity-check the app actually exists and contains our binary.
if [[ ! -x "$APP_PATH/Contents/MacOS/HomeIntelligenceHealth" ]]; then
  echo "Error: '$APP_PATH/Contents/MacOS/HomeIntelligenceHealth' not found." >&2
  echo "Build the app in Xcode first, then re-run this installer." >&2
  exit 1
fi

INTERVAL_SECONDS=$(( INTERVAL_MINUTES * 60 ))
if (( INTERVAL_SECONDS < 60 )); then
  INTERVAL_SECONDS=60
fi

TMP_PLIST="$(mktemp)"
sed \
  -e "s|__APP_PATH__|$APP_PATH|g" \
  -e "s|__ORCHESTRATOR_URL__|$ORCHESTRATOR_URL|g" \
  -e "s|__HEALTHKIT_TOKEN__|$HEALTHKIT_TOKEN|g" \
  -e "s|__MEMBER_ID__|$MEMBER_ID|g" \
  -e "s|__WINDOW_MINUTES__|$WINDOW_MINUTES|g" \
  -e "s|__INTERVAL_SECONDS__|$INTERVAL_SECONDS|g" \
  -e "s|__HOME__|$HOME|g" \
  "$TEMPLATE" > "$TMP_PLIST"

mkdir -p "$HOME/Library/LaunchAgents"
mv "$TMP_PLIST" "$PLIST"
chmod 600 "$PLIST"

# Reload the agent (bootout is idempotent — failure means it wasn't loaded).
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"

echo
echo "Installed and started: $LABEL"
echo "  Plist:    $PLIST"
echo "  App:      $APP_PATH"
echo "  Logs:     $HOME/Library/Logs/HomeIntelligenceHealth.log"
echo "  Schedule: every $INTERVAL_MINUTES min (window=$WINDOW_MINUTES min back)"
echo
echo "Run a one-shot test now:"
echo "  ORCHESTRATOR_URL='$ORCHESTRATOR_URL' \\"
echo "  HEALTHKIT_TOKEN='<token>' \\"
echo "  WINDOW_MINUTES='$WINDOW_MINUTES' \\"
echo "    '$APP_PATH/Contents/MacOS/HomeIntelligenceHealth'"
echo
echo "Tail the log:"
echo "  tail -f \"$HOME/Library/Logs/HomeIntelligenceHealth.log\""
