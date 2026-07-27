#!/bin/sh
# Reinstall the system libraries Chromium needs inside the OpenClaw container.
#
# WHY THIS EXISTS
# The Playwright browser binaries live under ~/.cache/ms-playwright, which is on
# the persistent config volume and survives container recreation. The system
# libraries they link against (libglib, libnss, libcairo, fonts, ...) are
# installed with apt into the container's overlay filesystem, which does NOT
# survive recreation. Re-run this after any TrueNAS app update or redeploy that
# recreates the container.
#
# RUN FROM THE HOST (pipe it in; root cannot read /home/node, see quirk 3):
#   cat install-browser-deps.sh | docker exec -i --user root ix-openclaw-openclaw-1 sh -s
#
# THEN VERIFY AS THE NODE USER:
#   docker exec ix-openclaw-openclaw-1 sh -c \
#     '/home/node/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome --version'
#
# THREE CONTAINER QUIRKS THIS WORKS AROUND
#  1. The container runs with CapDrop=ALL, so even root has no CAP_DAC_OVERRIDE.
#     apt's default download dir (/var/cache/apt/archives/partial) is mode 0700
#     owned by _apt, so root cannot write to it. We point apt at /tmp instead.
#  2. apt's privilege-dropping sandbox calls setgroups(), which is blocked for
#     the same reason. We disable it.
#  3. For that same reason root cannot read node's 0700 home directory, so this
#     script cannot test the browser binary itself - it checks for the shared
#     libraries instead and leaves the browser check to the node user.
set -e

echo "Configuring apt for a capability-less root..."
mkdir -p /tmp/aptcache/partial /tmp/aptlists/partial
chmod -R 777 /tmp/aptcache /tmp/aptlists
cat > /etc/apt/apt.conf.d/99-openclaw-browser <<'EOF'
APT::Sandbox::User "root";
Dir::Cache::archives "/tmp/aptcache";
Dir::State::lists "/tmp/aptlists";
EOF

echo "Updating package lists..."
DEBIAN_FRONTEND=noninteractive apt-get update -qq

echo "Installing Chromium system dependencies..."
cd /app
DEBIAN_FRONTEND=noninteractive node node_modules/playwright/cli.js install-deps chromium || true

# Font packages can fail their first postinst pass; a second configure sweep
# settles them once their dependencies are unpacked.
echo "Finishing package configuration..."
DEBIAN_FRONTEND=noninteractive dpkg --configure -a || true

echo "Verifying shared libraries..."
missing=""
for lib in libglib-2.0.so.0 libnss3.so libcairo.so.2 libpango-1.0.so.0 libatk-1.0.so.0; do
  ldconfig -p | grep -q "$lib" || missing="$missing $lib"
done
if [ -n "$missing" ]; then
  echo "FAIL: still missing:$missing"
  exit 1
fi
echo "PASS: Chromium shared libraries present."
