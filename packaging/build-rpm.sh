#!/bin/bash
#
# Build an .rpm package for PiForge (Fedora/RHEL-family; see the spec file
# for openSUSE naming caveats).
#
# Usage:  ./packaging/build-rpm.sh
# Output: packaging/rpm-dist/piforge-<version>-1.<dist>.noarch.rpm
#
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SELF_DIR}/.." && pwd)"

command -v rpmbuild >/dev/null || {
    echo "ERROR: rpmbuild not found."
    echo "  Fedora/RHEL: sudo dnf install rpm-build"
    echo "  Debian/Ubuntu (cross-building, as this project's own CI does): sudo apt install rpm"
    exit 1
}

VERSION="$(cat "${REPO_DIR}/VERSION")"
PKG="piforge"

RPMBUILD_ROOT="${SELF_DIR}/rpm-build"
DIST_DIR="${SELF_DIR}/rpm-dist"
SRC_STAGE="${RPMBUILD_ROOT}/${PKG}-${VERSION}"

rm -rf "$RPMBUILD_ROOT" "$DIST_DIR"
mkdir -p "$RPMBUILD_ROOT"/{BUILD,RPMS,SOURCES,SPECS,SRPMS} "$SRC_STAGE" "$DIST_DIR"

# --- source tarball: same app files as build-deb.sh, plus the packaging/
#     assets the spec's %install step reaches into directly (packaging/piforge,
#     packaging/piforge-server-root, packaging/debian/*, packaging/piforge.svg) ---
cp -p "${REPO_DIR}"/server.py \
      "${REPO_DIR}"/index.html \
      "${REPO_DIR}"/firstrun_gen.py \
      "${REPO_DIR}"/clone.py \
      "${REPO_DIR}"/cloning.py \
      "${REPO_DIR}"/blockmap.py \
      "${REPO_DIR}"/share_image.py \
      "${REPO_DIR}"/localimages.py \
      "${REPO_DIR}"/PRODUCTION.md \
      "${REPO_DIR}"/dashboard-finalize.sh \
      "${REPO_DIR}"/config.example.json \
      "${REPO_DIR}"/profiles.example.json \
      "${REPO_DIR}"/flash-all.sh \
      "${REPO_DIR}"/inject-config.sh \
      "${REPO_DIR}"/check-requirements.sh \
      "${REPO_DIR}"/README.md \
      "${REPO_DIR}"/LICENSE \
      "$SRC_STAGE/"
mkdir -p "$SRC_STAGE/packaging/debian"
cp -p "${SELF_DIR}/piforge" "${SELF_DIR}/piforge-server-root" "${SELF_DIR}/piforge.svg" "$SRC_STAGE/packaging/"
cp -p "${SELF_DIR}/debian/piforge.desktop" \
      "${SELF_DIR}/debian/io.github.gokul-hastrophil.piforge.policy" \
      "$SRC_STAGE/packaging/debian/"

tar -C "$RPMBUILD_ROOT" -czf "$RPMBUILD_ROOT/SOURCES/${PKG}-${VERSION}.tar.gz" "${PKG}-${VERSION}"

rpmbuild --define "_topdir ${RPMBUILD_ROOT}" \
         --define "piforge_version ${VERSION}" \
         -bb "${SELF_DIR}/rpm/piforge.spec"

find "$RPMBUILD_ROOT/RPMS" -name '*.rpm' -exec cp -p {} "$DIST_DIR/" \;

# `$(ls ...)` below can silently expand to nothing if rpmbuild didn't
# actually produce anything (ls failing doesn't fail the enclosing
# command substitution) — check explicitly instead of trusting the glob.
rpms=("$DIST_DIR"/*.rpm)
if [[ ! -f "${rpms[0]}" ]]; then
    echo "ERROR: rpmbuild completed without producing an RPM." >&2
    exit 1
fi

echo
printf 'Built:\n'
printf '  %s\n' "${rpms[@]}"
printf 'Install with:   sudo dnf install'
printf ' %q' "${rpms[@]}"
printf '\n'
echo "Remove with:    sudo dnf remove piforge"
