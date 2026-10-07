#!/usr/bin/env bash
#
# Build the arm64 kernel provision.py boots SD cards with inside QEMU.
#
# The Raspberry Pi's own kernel can't boot on QEMU's `virt` board, and the
# card's /lib/modules only has Pi kernels — so this kernel has NO modules:
# everything (virtio disk/net/9p, Docker's overlay/netfilter/bridge/veth,
# Tailscale's tun, cgroups, ext4/vfat) is built in (=y). One kernel serves
# every card; build it once.
#
# Usage:
#   ./vm-kernel/build-vm-kernel.sh               # -> ~/.cache/piforge/vm-kernel/Image
#   KVER=6.12.20 OUT=/some/dir ./vm-kernel/build-vm-kernel.sh
#
# Needs (Ubuntu/Debian):
#   sudo apt install gcc-aarch64-linux-gnu make flex bison bc libssl-dev libelf-dev xz-utils curl
#
set -euo pipefail

KVER="${KVER:-6.12.20}"
OUT="${OUT:-$HOME/.cache/piforge/vm-kernel}"
JOBS="${JOBS:-$(nproc)}"
SRC="$OUT/linux-$KVER"
CROSS="aarch64-linux-gnu-"
MAKE=(make -C "$SRC" ARCH=arm64 CROSS_COMPILE="$CROSS")

for t in "${CROSS}gcc" make flex bison bc xz curl; do
    command -v "$t" >/dev/null || {
        echo "ERROR: $t not found — sudo apt install gcc-aarch64-linux-gnu make flex bison bc libssl-dev libelf-dev xz-utils curl"
        exit 1
    }
done

mkdir -p "$OUT"
if [ ! -d "$SRC" ]; then
    echo ">>> Downloading Linux $KVER..."
    curl -fL "https://cdn.kernel.org/pub/linux/kernel/v${KVER%%.*}.x/linux-$KVER.tar.xz" -o "$OUT/linux-$KVER.tar.xz"
    tar -xJf "$OUT/linux-$KVER.tar.xz" -C "$OUT"
    rm -f "$OUT/linux-$KVER.tar.xz"
fi

echo ">>> Configuring..."
"${MAKE[@]}" defconfig

# What a Raspberry Pi OS userland + Docker + Tailscale needs on QEMU virt.
# Most are already in arm64 defconfig (some as =m); mod2yesconfig below then
# turns every remaining module into built-in.
ENABLE=(
    # QEMU virt devices
    PCI PCI_HOST_GENERIC VIRTIO VIRTIO_PCI VIRTIO_MMIO VIRTIO_BLK VIRTIO_NET
    VIRTIO_CONSOLE HW_RANDOM_VIRTIO SERIAL_AMBA_PL011 SERIAL_AMBA_PL011_CONSOLE RTC_DRV_PL031
    # The VM's screen in PiForge (virtio-gpu + VNC): display, keyboard, mouse
    DRM DRM_VIRTIO_GPU DRM_FBDEV_EMULATION FRAMEBUFFER_CONSOLE VIRTIO_INPUT INPUT_EVDEV
    NET_9P NET_9P_VIRTIO 9P_FS 9P_FS_POSIX_ACL
    # Filesystems
    EXT4_FS EXT4_FS_POSIX_ACL VFAT_FS FAT_DEFAULT_UTF8 NLS_CODEPAGE_437 NLS_ISO8859_1 NLS_UTF8
    OVERLAY_FS FUSE_FS TMPFS TMPFS_POSIX_ACL AUTOFS_FS DEVTMPFS DEVTMPFS_MOUNT
    # systemd / containers
    CGROUPS MEMCG BLK_CGROUP CGROUP_PIDS CGROUP_FREEZER CGROUP_DEVICE CGROUP_CPUACCT
    CGROUP_SCHED CPUSETS CGROUP_BPF BPF_SYSCALL NAMESPACES USER_NS PID_NS NET_NS IPC_NS UTS_NS
    SECCOMP SECCOMP_FILTER KEYS POSIX_MQUEUE FHANDLE
    # Docker networking
    BRIDGE BRIDGE_NETFILTER VETH MACVLAN IPVLAN VXLAN DUMMY TUN
    NETFILTER NETFILTER_ADVANCED NF_CONNTRACK NF_NAT NF_TABLES NF_TABLES_INET NF_TABLES_IPV4
    NF_TABLES_IPV6 NFT_COMPAT NFT_NAT NFT_MASQ NFT_CT NFT_CHAIN_NAT
    NETFILTER_XT_MATCH_ADDRTYPE NETFILTER_XT_MATCH_CONNTRACK NETFILTER_XT_MATCH_IPVS
    NETFILTER_XT_MARK NETFILTER_XT_TARGET_MASQUERADE NETFILTER_XT_MATCH_COMMENT
    IP_NF_IPTABLES IP_NF_FILTER IP_NF_NAT IP_NF_TARGET_MASQUERADE IP_NF_MANGLE IP_NF_RAW
    IP6_NF_IPTABLES IP6_NF_FILTER IP6_NF_NAT IP6_NF_TARGET_MASQUERADE IP6_NF_MANGLE
    IP_ADVANCED_ROUTER IP_MULTIPLE_TABLES IPV6_MULTIPLE_TABLES
)
for opt in "${ENABLE[@]}"; do
    "$SRC/scripts/config" --file "$SRC/.config" --enable "$opt"
done
"${MAKE[@]}" olddefconfig
"${MAKE[@]}" mod2yesconfig
"${MAKE[@]}" olddefconfig

echo ">>> Building (-j$JOBS; 15-40 min the first time)..."
"${MAKE[@]}" -j"$JOBS" Image

cp "$SRC/arch/arm64/boot/Image" "$OUT/Image"
echo "$KVER" > "$OUT/Image.version"
echo ">>> Done: $OUT/Image (Linux $KVER)"
