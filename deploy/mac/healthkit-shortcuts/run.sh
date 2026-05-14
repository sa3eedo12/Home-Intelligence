#!/usr/bin/env bash
# Run the "HI Health Snapshot" Shortcut and pipe its JSON output to the
# Python forwarder which POSTs it to TrueNAS. This script is what launchd
# invokes on a schedule.
#
# Usage (manual / testing):
#   ORCHESTRATOR_URL=http://truenas.local:8080 \
#   HEALTHKIT_TOKEN=<token> \
#     ./run.sh
#
# When invoked by launchd, the env vars come from the LaunchAgent plist
# installed by ./install.sh.

set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
SHORTCUT_NAME="${SHORTCUT_NAME:-HI Health Snapshot}"
LOG="$HOME/Library/Logs/healthkit-shortcuts.log"
mkdir -p "$(dirname "$LOG")"

log() { printf '%s [run.sh] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }

if ! command -v shortcuts >/dev/null 2>&1; then
  log "FATAL: 'shortcuts' CLI not found — needs macOS Monterey 12+"
  exit 2
fi

# Capture both stdout (the JSON) and stderr (Shortcuts errors). Because
# `shortcuts run` will sometimes print warnings to stderr that we don't want
# to feed into the forwarder, we keep the streams separate.
SNAPSHOT="$(shortcuts run "$SHORTCUT_NAME" 2>>"$LOG" || true)"

if [[ -z "$SNAPSHOT" ]]; then
  log "Shortcut '$SHORTCUT_NAME' produced no output (not installed yet, or returned nothing this run)"
  exit 0
fi

printf '%s' "$SNAPSHOT" | /usr/bin/python3 "$SCRIPT_DIR/forwarder.py"
exit $?
