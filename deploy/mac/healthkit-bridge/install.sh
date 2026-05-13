#!/usr/bin/env bash
# Install the Health Auto Export -> Home Intelligence bridge as a launchd
# LaunchAgent. Idempotent: re-running updates the agent in place.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
TEMPLATE="$SCRIPT_DIR/com.home-intelligence.healthkit-bridge.plist.template"
BRIDGE="$SCRIPT_DIR/bridge.py"

LABEL="com.home-intelligence.healthkit-bridge"
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

prompt ORCHESTRATOR_URL "TrueNAS orchestrator URL" "http://truenas.local:8080"
prompt HEALTHKIT_TOKEN  "HEALTHKIT_WEBHOOK_TOKEN value (the same one set on TrueNAS)"
prompt WATCH_DIR        "Folder to watch (where Health Auto Export drops JSON)" \
                        "$HOME/Library/Mobile Documents/com~apple~CloudDocs/HealthAutoExport"
prompt MEMBER_ID        "Optional household_members.id to attribute uploads to (blank to auto-resolve)" ""

mkdir -p "$WATCH_DIR" "$WATCH_DIR/processed" "$WATCH_DIR/failed"
chmod +x "$BRIDGE"

# Render the plist with envsubst-style replacements (without depending on envsubst)
TMP_PLIST="$(mktemp)"
sed \
  -e "s|__BRIDGE_PATH__|$BRIDGE|g" \
  -e "s|__ORCHESTRATOR_URL__|$ORCHESTRATOR_URL|g" \
  -e "s|__HEALTHKIT_TOKEN__|$HEALTHKIT_TOKEN|g" \
  -e "s|__WATCH_DIR__|$WATCH_DIR|g" \
  -e "s|__MEMBER_ID__|$MEMBER_ID|g" \
  -e "s|__HOME__|$HOME|g" \
  "$TEMPLATE" > "$TMP_PLIST"

mkdir -p "$HOME/Library/LaunchAgents"
mv "$TMP_PLIST" "$PLIST"
chmod 600 "$PLIST"

# Reload the agent
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"

echo
echo "Installed and started: $LABEL"
echo "  Plist:   $PLIST"
echo "  Logs:    $HOME/Library/Logs/healthkit-bridge.log"
echo "  Watch:   $WATCH_DIR"
echo
echo "Run a one-shot test:"
echo "  ORCHESTRATOR_URL='$ORCHESTRATOR_URL' HEALTHKIT_TOKEN='<token>' \\"
echo "    WATCH_DIR='$WATCH_DIR' /usr/bin/python3 '$BRIDGE'"
