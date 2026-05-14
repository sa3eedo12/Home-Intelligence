#!/usr/bin/env bash
# Build HomeIntelligenceHealth.app from the included Xcode project for an
# iOS device. Run this AFTER setting your Apple Developer team via either:
#   - Xcode GUI (recommended): open HomeIntelligenceHealth.xcodeproj →
#     target → Signing & Capabilities → pick your team → ⌘S
#   - or pass it inline: DEVELOPMENT_TEAM=ABC123XYZ4 ./build.sh
#
# After it builds, install to your iPhone via:
#   - Xcode: select your iPhone in the run-destination dropdown → ⌘R
#     (xcodebuild can also do it: see "install to device" at the bottom)
#
# The HealthKit entitlement requires Apple Developer team-managed
# automatic signing — that's why this script can't sign for you without
# your team id.

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
  -sdk iphoneos \
  -destination 'generic/platform=iOS' \
  ${DEVELOPMENT_TEAM:+DEVELOPMENT_TEAM="$DEVELOPMENT_TEAM"} \
  -derivedDataPath "$DERIVED" \
  -allowProvisioningUpdates \
  build

APP="$DERIVED/Build/Products/${CONFIG}-iphoneos/HomeIntelligenceHealth.app"
if [[ ! -d "$APP" ]]; then
  echo "Build reported success but $APP is missing." >&2
  exit 1
fi

cat <<DONE

Built: $APP

Install to your iPhone:
  - Easiest: open the project in Xcode, select your iPhone in the
    destination dropdown, and press ⌘R.
  - Or use the command line:
      xcrun devicectl device install app --device <udid> "$APP"
    (find your UDID with: xcrun devicectl list devices)

After it's on your phone, open the app once to fill in the TrueNAS URL
and HealthKit token, tap "Sync now" to grant Health permission, then
schedule it via the Shortcuts app — recipe in README.md.
DONE
