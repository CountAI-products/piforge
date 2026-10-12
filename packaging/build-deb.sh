#!/bin/bash
#
# Build a .deb package for PiForge: an installable system app, like
# Raspberry Pi Imager's own .deb — /opt/piforge holds the app, /usr/bin/piforge
# is the desktop launcher, plus a .desktop entry and icon.
#
# Usage:  ./packaging/build-deb.sh
# Output: packaging/dist/piforge_<version>_all.deb
#
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SELF_DIR}/.." && pwd)"

command -v dpkg-deb >/dev/null || { echo "ERROR: dpkg-deb not found (sudo apt install dpkg-dev)"; exit 1; }

VERSION="$(cat "${REPO_DIR}/VERSION")"
PKG="piforge"
ARCH="all"
BUILD_DIR="${SELF_DIR}/build/${PKG}_${VERSION}_${ARCH}"
DIST_DIR="${SELF_DIR}/dist"

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/DEBIAN" \
         "$BUILD_DIR/opt/piforge" \
         "$BUILD_DIR/usr/bin" \
         "$BUILD_DIR/usr/share/applications" \
         "$BUILD_DIR/usr/share/icons/hicolor/scalable/apps" \
         "$BUILD_DIR/usr/share/polkit-1/actions"

# --- app files (never config.json/profiles.json — those hold real secrets
#     and are gitignored; the packaged app ships only the .example templates) ---
cp -p "${REPO_DIR}"/server.py \
      "${REPO_DIR}"/index.html \
      "${REPO_DIR}"/firstrun_gen.py \
      "${REPO_DIR}"/clone.py \
      "${REPO_DIR}"/cloning.py \
      "${REPO_DIR}"/localimages.py \
      "${REPO_DIR}"/PRODUCTION.md \
      "${REPO_DIR}"/dashboard-finalize.sh \
      "${REPO_DIR}"/config.example.json \
      "${REPO_DIR}"/profiles.example.json \
      "${REPO_DIR}"/tailscale_config.example.json \
      "${REPO_DIR}"/flash-all.sh \
      "${REPO_DIR}"/inject-config.sh \
      "${REPO_DIR}"/check-requirements.sh \
      "${REPO_DIR}"/README.md \
      "${REPO_DIR}"/LICENSE \
      "$BUILD_DIR/opt/piforge/"

install -m 0755 "${SELF_DIR}/piforge" "$BUILD_DIR/usr/bin/piforge"
install -m 0755 "${SELF_DIR}/piforge-server-root" "$BUILD_DIR/opt/piforge/piforge-server-root"
install -m 0644 "${SELF_DIR}/debian/piforge.desktop" "$BUILD_DIR/usr/share/applications/piforge.desktop"
install -m 0644 "${SELF_DIR}/piforge.svg" "$BUILD_DIR/usr/share/icons/hicolor/scalable/apps/piforge.svg"
install -m 0644 "${SELF_DIR}/debian/io.github.gokul-hastrophil.piforge.policy" \
    "$BUILD_DIR/usr/share/polkit-1/actions/io.github.gokul-hastrophil.piforge.policy"

INSTALLED_SIZE_KB=$(du -sk "$BUILD_DIR/opt" "$BUILD_DIR/usr" | awk '{sum+=$1} END {print sum}')

cat > "$BUILD_DIR/DEBIAN/control" <<EOF
Package: ${PKG}
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: ${ARCH}
Installed-Size: ${INSTALLED_SIZE_KB}
Depends: python3, python3-gi, gir1.2-gtk-3.0, gir1.2-webkit2-4.1, util-linux, coreutils, xz-utils, openssl, wpasupplicant, parted, dosfstools, e2fsprogs (>= 1.47), udev, gnupg, jq, curl, pkexec, polkitd
Maintainer: PiForge contributors <noreply@example.invalid>
Homepage: https://github.com/CountAI-products/piforge
Description: Mass SD card installer for Raspberry Pi OS
 PiForge flashes Raspberry Pi OS to many SD cards in parallel with
 hostname, user, password, Wi-Fi, SSH keys, static IP, and other
 first-boot settings pre-configured, so every card boots straight to a
 working login with no setup wizard.
 .
 Decompresses the OS image once and writes every selected card
 concurrently from the page cache for near-linear speedup with card
 count. Includes named config profiles, a flash history log, and
 per-card cancel/retry.
EOF

dpkg-deb --build --root-owner-group "$BUILD_DIR" \
    "$(mkdir -p "$DIST_DIR" && echo "$DIST_DIR")/${PKG}_${VERSION}_${ARCH}.deb"

echo
echo "Built: ${DIST_DIR}/${PKG}_${VERSION}_${ARCH}.deb"
echo "Install with:   sudo apt install ${DIST_DIR}/${PKG}_${VERSION}_${ARCH}.deb"
echo "Remove with:    sudo apt remove ${PKG}"
