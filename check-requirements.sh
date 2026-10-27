#!/bin/bash
#
# Verify the host has everything PiForge needs. Run this once after
# cloning, or whenever something behaves unexpectedly.
#
set -uo pipefail

ok=1
need() {
    if command -v "$1" >/dev/null 2>&1; then
        echo "  OK   $1"
    else
        echo "  MISS $1  ($2)"
        ok=0
    fi
}

echo "Checking required tools..."
need python3     "sudo apt install python3"
need lsblk        "sudo apt install util-linux"
need dd           "sudo apt install coreutils"
need xz           "sudo apt install xz-utils"
need openssl      "sudo apt install openssl"
need wpa_passphrase "sudo apt install wpasupplicant"
need partprobe    "sudo apt install parted"
need mkfs.vfat    "sudo apt install dosfstools"
need mkfs.ext4    "sudo apt install e2fsprogs"
need resize2fs    "sudo apt install e2fsprogs"
need e2fsck       "sudo apt install e2fsprogs"
need tune2fs      "sudo apt install e2fsprogs"
need jq           "sudo apt install jq"
need curl         "sudo apt install curl"
need gpg          "sudo apt install gnupg"
need shred        "sudo apt install coreutils"
need udevadm      "sudo apt install udev"

echo
echo "Optional (only needed for flash-all.sh, the CLI-only path):"
if command -v rpi-imager >/dev/null 2>&1; then
    echo "  OK   rpi-imager"
else
    echo "  MISS rpi-imager  (sudo apt install rpi-imager, or: sudo snap install rpi-imager)"
fi

echo
e2fs_version="$(e2fsck -V 2>&1 | sed -n 's/^e2fsck \([0-9][0-9.]*\).*/\1/p' | head -1)"
lowest_version="$(printf '%s\n' 1.47 "$e2fs_version" | sort -V | head -1)"
if [ -n "$e2fs_version" ] && [ "$lowest_version" = 1.47 ]; then
    echo "  OK   e2fsprogs $e2fs_version (clone-safe)"
else
    echo "  MISS e2fsprogs 1.47+ required for power-loss-safe cloning (found ${e2fs_version:-unknown})"
    ok=0
fi

echo
if [ "$(uname -s)" != "Linux" ]; then
    echo "WARNING: this tool relies on Linux-specific interfaces (lsblk, /sys/block,"
    echo "dd oflag=direct, partprobe). It will not work on macOS or Windows as-is."
    ok=0
fi

echo
if [ ! -f "$(dirname "$0")/config.json" ]; then
    echo "NOTE: config.json not found — copy config.example.json to config.json"
    echo "      and set your real hostname/user/password/Wi-Fi before flashing."
fi

echo
if [ "$ok" = 1 ]; then
    echo "All required tools present."
else
    echo "Some required tools are missing — install them, then re-run this script."
    exit 1
fi
