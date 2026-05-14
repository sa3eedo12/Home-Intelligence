#!/usr/bin/env bash
# Install the HealthKit Shortcuts -> Home Intelligence bridge as a launchd
# LaunchAgent. Idempotent: re-running updates the agent in place.
#
# This is the FREE alternative to ./healthkit-bridge/ — it uses macOS
# Shortcuts to read HealthKit (no paid iOS app required), then forwards via
# this same launchd pattern. Build the "HI Health Snapshot" Shortcut once
# in the macOS Shortcuts app following the recipe in README.md, then run
# this installer.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
TEMPLATE="$SCRIPT_DIR/com.home-intelligence.healthkit-shortcuts.plist.template"
RUN_SCRIPT="$SCRIPT_DIR/run.sh"
FORWARDER="$SCRIPT_DIR/forwarder.py"

LABEL="com.home-intelligence.healthkit-shortcuts"
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

# Sanity checks before we touch anything.
if ! command -v shortcuts >/dev/null 2>&1; then
  echo "Error: 'shortcuts' CLI not found. Requires macOS Monterey 12 or newer." >&2
  exit 1
fi

prompt ORCHESTRATOR_URL "TrueNAS orchestrator URL" "http://truenas.local:8080"
prompt HEALTHKIT_TOKEN  "HEALTHKIT_WEBHOOK_TOKEN value (the same one set on TrueNAS)"
prompt SHORTCUT_NAME    "Name of the macOS Shortcut you built" "HI Health Snapshot"
prompt INTERVAL_MINUTES "Run the Shortcut every N minutes" "15"
prompt MEMBER_ID        "Optional household_members.id to attribute uploads to (blank to auto-resolve)" ""

# Validate the Shortcut is installed.
if ! shortcuts list 2>/dev/null | grep -Fxq "$SHORTCUT_NAME"; then
  echo
  echo "Warning: shortcut '$SHORTCUT_NAME' is not installed."
  echo "  Build it in the macOS Shortcuts app following the recipe in README.md,"
  echo "  then re-run this installer (or proceed and install the Shortcut later)."
  echo
  read -r -p "Continue anyway? [y/N]: " reply
  if [[ "${reply:-N}" != [Yy]* ]]; then
    exit 1
  fi
fi

INTERVAL_SECONDS=$(( INTERVAL_MINUTES * 60 ))
if (( INTERVAL_SECONDS < 60 )); then
  INTERVAL_SECONDS=60
fi

chmod +x "$RUN_SCRIPT" "$FORWARDER"

# Render the plist with envsubst-style replacements (without depending on envsubst).
TMP_PLIST="$(mktemp)"
sed \
  -e "s|__RUN_PATH__|$RUN_SCRIPT|g" \
  -e "s|__ORCHESTRATOR_URL__|$ORCHESTRATOR_URL|g" \
  -e "s|__HEALTHKIT_TOKEN__|$HEALTHKIT_TOKEN|g" \
  -e "s|__MEMBER_ID__|$MEMBER_ID|g" \
  -e "s|__SHORTCUT_NAME__|$SHORTCUT_NAME|g" \
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
echo "  Logs:     $HOME/Library/Logs/healthkit-shortcuts.log"
echo "  Schedule: every $INTERVAL_MINUTES min (StartInterval=$INTERVAL_SECONDS)"
echo "  Shortcut: $SHORTCUT_NAME"
echo
echo "Run a one-shot test now:"
echo "  ORCHESTRATOR_URL='$ORCHESTRATOR_URL' \\"
echo "  HEALTHKIT_TOKEN='<token>' \\"
echo "  SHORTCUT_NAME='$SHORTCUT_NAME' \\"
echo "    '$RUN_SCRIPT'"
echo
echo "Tail the log:"
echo "  tail -f \"$HOME/Library/Logs/healthkit-shortcuts.log\""
