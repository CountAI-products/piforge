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
      "${REPO_DIR}"/config.example.json \
      "${REPO_DIR}"/profiles.example.json \
      "${REPO_DIR}"/flash-all.sh \
      "${REPO_DIR}"/inject-config.sh \
      "${REPO_DIR}"/check-requirements.sh \
      "${REPO_DIR}"/README.md \
      "${REPO_DIR}"/LICENSE \
      "${REPO_DIR}"/provision.py \
      "${REPO_DIR}"/clone.py \
      "${REPO_DIR}"/provision.example.json \
      "$BUILD_DIR/opt/piforge/"
install -D -m 0755 "${REPO_DIR}/vm-kernel/build-vm-kernel.sh" "$BUILD_DIR/opt/piforge/vm-kernel/build-vm-kernel.sh"
# Bundled noVNC (MPL-2.0) — the Full install tab's VM desktop viewer, so it works offline.
mkdir -p "$BUILD_DIR/opt/piforge/vendor"
cp -r "${REPO_DIR}/vendor/novnc" "$BUILD_DIR/opt/piforge/vendor/"

# Optional extras for a ready-to-hand-out station build (neither is in git):
#  - the prebuilt VM kernel, so stations never need to build one
#    (VM_KERNEL=/path/to/Image, default: your ~/.cache/piforge/vm-kernel/Image)
#  - provision.defaults.json, your site's Full install settings preset.
#    It may contain the card password / Tailscale key: only hand that .deb
#    to people who may have them.
VM_KERNEL="${VM_KERNEL:-$HOME/.cache/piforge/vm-kernel/Image}"
if [ -f "$VM_KERNEL" ]; then
    install -m 0644 "$VM_KERNEL" "$BUILD_DIR/opt/piforge/vm-kernel/Image"
    echo "Including VM kernel: $VM_KERNEL"
fi
if [ -f "${REPO_DIR}/provision.defaults.json" ]; then
    install -m 0644 "${REPO_DIR}/provision.defaults.json" "$BUILD_DIR/opt/piforge/provision.defaults.json"
    echo "Including provision.defaults.json (site preset — may contain secrets)"
fi

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
Depends: python3, python3-gi, gir1.2-webkit2-4.1 | gir1.2-webkit2-4.0, util-linux, coreutils, e2fsprogs, xz-utils, openssl, wpasupplicant, parted, jq, curl, policykit-1
Recommends: qemu-system-arm, e2fsprogs, openssh-client
Suggests: gcc-aarch64-linux-gnu, make, flex, bison, bc, libssl-dev, libelf-dev
Maintainer: PiForge contributors <noreply@example.invalid>
Homepage: https://github.com/
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
