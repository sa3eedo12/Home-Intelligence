#!/usr/bin/env bash
# Build HomeIntelligenceHealth.app from the included Xcode project. Run
# this AFTER setting your Apple Developer team via either:
#   - Xcode GUI: open HomeIntelligenceHealth.xcodeproj → target settings →
#     Signing & Capabilities → pick your team. Then save.
#   - or pass it inline: DEVELOPMENT_TEAM=ABC123XYZ4 ./build.sh
#
# Output: ./build/Release/HomeIntelligenceHealth.app
#
# The HealthKit entitlement requires team-managed automatic signing,
# which is why this script can't do it for you without your team id.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
cd "$SCRIPT_DIR"

CONFIG="${CONFIG:-Release}"
DERIVED="${SCRIPT_DIR}/build"

if [[ -z "${DEVELOPMENT_TEAM:-}" ]]; then
  cat <<'HINT'
DEVELOPMENT_TEAM is not set.

To find your team id:
  1. Open https://developer.apple.com/account
  2. Look under "Membership" → "Team ID" (10 character string)

Then re-run with it set, or pre-configure it in the Xcode GUI:
  open HomeIntelligenceHealth.xcodeproj
  (target → Signing & Capabilities → pick your team, then save)

Continuing with whatever team Xcode has saved in the project (may be none).
HINT
fi

xcodebuild \
  -project HomeIntelligenceHealth.xcodeproj \
  -scheme HomeIntelligenceHealth \
  -configuration "$CONFIG" \
  ${DEVELOPMENT_TEAM:+DEVELOPMENT_TEAM="$DEVELOPMENT_TEAM"} \
  -derivedDataPath "$DERIVED" \
  -allowProvisioningUpdates \
  build

APP="$DERIVED/Build/Products/$CONFIG/HomeIntelligenceHealth.app"
if [[ ! -d "$APP" ]]; then
  echo "Build reported success but $APP is missing." >&2
  exit 1
fi

echo
echo "Built: $APP"
echo
echo "Copy to a stable location:"
echo "  cp -R '$APP' ~/Applications/"
echo
echo "Then run once manually to grant HealthKit permission:"
echo "  ORCHESTRATOR_URL=http://truenas.local:8080 \\"
echo "  HEALTHKIT_TOKEN=<token> \\"
echo "    ~/Applications/HomeIntelligenceHealth.app/Contents/MacOS/HomeIntelligenceHealth"
echo
echo "Finally schedule it via launchd:"
echo "  ./install-launchd.sh"
