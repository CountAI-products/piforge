#!/usr/bin/env python3
"""
provision.py — take one SD card from blank to fully installed on this PC, by
booting it in a QEMU arm64 VM and running your install scripts inside it.

  flash      write the Raspberry Pi OS image (same image cache as the web UI)
  partition  grow root and add the extra partitions from "layout"
  inject     hostname / user / SSH keys (the same firstrun.sh the web UI
             writes) + disable Pi OS's first-boot auto-resize
  boot       boot the card in QEMU (first boot applies firstrun.sh and
             reboots once), then wait for SSH on 127.0.0.1:<ssh_port>
  install    mount the host shares, copy the install package in, run
             "install_cmd" (e.g. your install.sh in its PC stage)
  tailscale  install Tailscale and join with the auth key — the card keeps
             this identity and IP when it moves to the real Pi
  verify     run each "verify_cmds" entry; any failure fails the card
  finish     clean up the VM-only bits, power the VM off cleanly, flush

Anything that needs real Pi hardware can't run here: have install_cmd stage
it for the Pi's own first boot.

Usage:
  sudo python3 provision.py --config provision.json --hostname sn-001 /dev/sdX
  sudo python3 provision.py --config provision.json --hostname sn-001 /dev/sdX --from install

Needs: qemu-system-aarch64 (qemu-system-arm), the VM kernel from
vm-kernel/build-vm-kernel.sh, and the usual sfdisk/e2fsprogs/xz tools.
See provision.example.json for every config field.
"""

import argparse
import csv
import datetime
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager

import server
from firstrun_gen import make_firstrun

STEPS = ["flash", "partition", "inject", "boot", "install", "tailscale", "verify", "finish"]
VM_STEPS = {"boot", "install", "tailscale", "verify"}
SECTOR = 512

REGISTRY_PATH = os.path.join(server.IMAGES_DIR, "provisioned-cards.csv")
REGISTRY_FIELDS = ["timestamp", "device", "hostname", "tailscale_ip", "result", "detail", "log_dir"]

# VM-only files PiForge puts on the card and removes again in `finish`.
UDEV_RULE = "/etc/udev/rules.d/90-piforge-vm-disk.rules"
TEMP_SUDOERS = "/etc/sudoers.d/090-piforge-provision"
# Pi OS builds its initramfs with MODULES=dep, which works out the drivers from
# the running system: in the VM that's QEMU's disk and kernel, so mkinitramfs
# fails ("failed to determine device for /"). VM-only, removed in finish.
INITRAMFS_CONF = "/etc/initramfs-tools/conf.d/90-piforge-vm.conf"

DEFAULTS = {
    "image": server.DEFAULT_IMAGE_URL,
    "card": {},
    "layout": None,
    "vm": {
        "kernel": "~/.cache/piforge/vm-kernel/Image",
        "cpus": 4,
        "memory": "4G",
        "ssh_port": 2222,
        "disk_alias": "mmcblk0",
        "firstrun_timeout_min": 45,
        "ssh_timeout_min": 30,
        # A virtual display for the VM, streamed over VNC to the app's Screen
        # window (the Pi OS desktop). Costs CPU under emulation.
        "screen": False,
    },
    "shares": [],
    "package": None,
    "install_cmd": "",
    "install_env": {},
    "tailscale": None,
    "verify_cmds": [],
}


class ProvisionError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def real_ids():
    """uid/gid of the user behind sudo/pkexec, so logs, keys and the registry
    under their home stay theirs rather than root's."""
    uid = os.environ.get("PKEXEC_UID") or os.environ.get("SUDO_UID")
    if not uid:
        return None
    import pwd
    pw = pwd.getpwuid(int(uid))
    return pw.pw_uid, pw.pw_gid


def chown_real(path):
    ids = real_ids()
    if ids:
        try:
            os.chown(path, *ids)
        except OSError:
            pass


def expand(path):
    """~ means the invoking user's home, not /root."""
    if path.startswith("~"):
        return os.path.join(server.real_home(), path[2:] if path.startswith("~/") else path[1:])
    return path


def resolve_kernel(cfg):
    """The configured VM kernel, else one bundled next to this script (the
    packaged app ships it in /opt/piforge/vm-kernel/ when built with one)."""
    for path in (expand(cfg["vm"]["kernel"]),
                 os.path.join(server.BASE_DIR, "vm-kernel", "Image")):
        if os.path.exists(path):
            return path
    return None


def part_path(dev, n):
    return f"{dev}p{n}" if dev[-1].isdigit() else f"{dev}{n}"


def parse_size(text):
    """'30G' / '512M' / '30GiB' -> bytes (binary units, like sfdisk)."""
    m = re.fullmatch(r"\s*(\d+)\s*([MG])(?:i?B)?\s*", str(text), re.I)
    if not m:
        raise ProvisionError(f"bad size {text!r} — use e.g. 512M or 30G, or \"rest\"")
    return int(m.group(1)) * (1 << (20 if m.group(2).upper() == "M" else 30))


class Log:
    def __init__(self, path):
        self.path = path
        self.f = open(path, "a", buffering=1)
        chown_real(path)

    def __call__(self, msg=""):
        line = f"[{datetime.datetime.now():%H:%M:%S}] {msg}"
        self._out(line + "\n")
        self.f.write(line + "\n")

    def raw(self, text):
        self._out(text)
        self.f.write(text)

    @staticmethod
    def _out(text):
        # The app reads our stdout; if it goes away mid-install, keep going
        # (the log file still has everything) so `finish` can still power
        # the VM off cleanly instead of dying on a broken pipe.
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except (OSError, ValueError):
            pass


def run(log, cmd, check=True, **kw):
    log("$ " + (cmd if isinstance(cmd, str) else " ".join(shlex.quote(c) for c in cmd)))
    r = subprocess.run(cmd, text=True, capture_output=True, **kw)
    for stream in (r.stdout, r.stderr):
        if stream and stream.strip():
            log.raw(stream if stream.endswith("\n") else stream + "\n")
    if check and r.returncode != 0:
        raise ProvisionError(f"command failed (exit {r.returncode}): {cmd}")
    return r


def flush_card_cache(log, dev):
    """Drop the kernel's cached blocks for the card and each of its partitions.
    dd (O_DIRECT, to the whole disk) and sfdisk bypass the partitions' own
    caches, so a partition mounted before (an earlier run, or an old card)
    can otherwise be read back stale -- e.g. a cmdline.txt full of zeros."""
    paths = subprocess.run(["lsblk", "-nlpo", "PATH", dev], capture_output=True, text=True).stdout.split()
    for path in paths or [dev]:
        run(log, ["blockdev", "--flushbufs", path], check=False)


@contextmanager
def mounted(log, part, readonly=False):
    subprocess.run(["umount", part], capture_output=True)  # desktop automount
    mnt = tempfile.mkdtemp(prefix="piforge-")
    try:
        run(log, ["mount", "-o", "ro" if readonly else "rw", part, mnt])
        yield mnt
    finally:
        subprocess.run(["sync"])
        subprocess.run(["umount", mnt], capture_output=True)
        os.rmdir(mnt)


def load_config(path):
    with open(path) as f:
        user = json.load(f)
    cfg = json.loads(json.dumps(DEFAULTS))
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


# ---------------------------------------------------------------- partition layout

def build_sfdisk_script(table, layout, dev_sectors):
    """Pure function (testable against a plain image file): keep p1 as-is,
    grow p2 in place, then add layout["partitions"]. Primaries fill slots
    3-4; if any partition is numbered 5+, the next free primary slot becomes
    an extended partition spanning the rest of the card and those become its
    logicals. label-id is kept, so the PARTUUIDs in cmdline.txt and
    /etc/fstab stay valid."""
    if table.get("label") != "dos":
        raise ProvisionError(f"expected an MBR (dos) partition table, found {table.get('label')!r}")
    parts = sorted(table["partitions"], key=lambda p: p["start"])
    if len(parts) != 2:
        raise ProvisionError(
            f"expected a fresh Raspberry Pi OS card (2 partitions), found {len(parts)} — "
            "reflash it (run from the 'flash' step)")
    p1, p2 = parts

    root_bytes = parse_size(layout["root_size"])
    root_sectors = root_bytes // SECTOR
    if root_sectors < p2["size"]:
        raise ProvisionError(
            f"root_size {layout['root_size']} is smaller than the image's root "
            f"partition ({p2['size'] * SECTOR >> 20} MiB) — it can only grow")

    extra = sorted(layout.get("partitions", []), key=lambda p: p["num"])
    nums = [p["num"] for p in extra]
    if len(set(nums)) != len(nums):
        raise ProvisionError(f"duplicate partition numbers in layout: {nums}")
    primaries = [p for p in extra if p["num"] < 5]
    logicals = [p for p in extra if p["num"] >= 5]
    if [p["num"] for p in primaries] != list(range(3, 3 + len(primaries))):
        raise ProvisionError("primary partitions must be numbered from 3 with no gaps (3, then 4)")
    if [p["num"] for p in logicals] != list(range(5, 5 + len(logicals))):
        raise ProvisionError("logical partitions must be numbered from 5 with no gaps")
    if logicals and len(primaries) > 1:
        raise ProvisionError("with logical partitions (5+), slot 4 is the extended partition — "
                             "at most one extra primary (3) fits")
    for i, p in enumerate(extra):
        if str(p["size"]).lower() == "rest" and i != len(extra) - 1:
            raise ProvisionError(f"only the last partition can be \"rest\" (p{p['num']} is not last)")

    # Explicit start sectors for everything new: left to itself, sfdisk puts a
    # start-less partition in the FIRST free gap — the few MiB before p1 on
    # every Pi OS image — not after p2.
    ALIGN = 2048  # 1 MiB

    def align(sector):
        return -(-sector // ALIGN) * ALIGN

    def fit(start, p, end):
        size = end - start if str(p["size"]).lower() == "rest" else parse_size(p["size"]) // SECTOR
        if size <= 0 or start + size > end:
            raise ProvisionError(
                f"layout does not fit: p{p['num']} would end at "
                f"{(start + max(size, 0)) * SECTOR / (1 << 30):.1f} GiB, card is "
                f"{end * SECTOR / (1 << 30):.1f} GiB")
        return size

    lines = [
        "label: dos",
        f"label-id: {table['id']}",
        "unit: sectors",
        "",
        f"start={p1['start']}, size={p1['size']}, type={p1['type']}"
        + (", bootable" if p1.get("bootable") else ""),
        f"start={p2['start']}, size={root_sectors}, type={p2['type']}",
    ]
    cur = align(p2["start"] + root_sectors)
    if cur > dev_sectors:
        raise ProvisionError(f"root_size {layout['root_size']} does not fit on this card")
    for p in primaries:
        size = fit(cur, p, dev_sectors)
        lines.append(f"start={cur}, size={size}, type=83")
        cur = align(cur + size)
    if logicals:
        # Extended spans the rest of the card; each logical needs room for its
        # own EBR in front of it.
        lines.append(f"start={cur}, size={dev_sectors - cur}, type=5")
        for p in logicals:
            start = cur + ALIGN
            size = fit(start, p, dev_sectors)
            lines.append(f"start={start}, size={size}, type=83")
            cur = align(start + size)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- the job

class Provisioner:
    def __init__(self, cfg, dev, hostname, log, log_dir):
        self.cfg = cfg
        self.dev = dev
        self.hostname = hostname
        self.log = log
        self.log_dir = log_dir
        self.vm = cfg["vm"]
        self.user = cfg["card"].get("user") or "pi"
        self.key = os.path.join(server.USER_CONFIG_DIR, "provision_ed25519")
        self.qemu = None
        self.tailscale_ip = ""
        self.failed_checks = []

    # ------------------------------------------------ flash
    def step_flash(self):
        image = self.cfg["image"]
        if re.match(r"https?://", image):
            self.log(f"Preparing image (cached in {server.IMAGES_DIR}): {image}")
            server.prepare_image(image)
            src, compressed = server.RAW_IMAGE, False
        else:
            src = expand(image)
            if not os.path.exists(src):
                raise ProvisionError(f"image not found: {src}")
            compressed = src.endswith(".xz")
        if not compressed and os.path.getsize(src) > server.device_size_bytes(self.dev):
            raise ProvisionError("card is smaller than the image")

        out = subprocess.run(["lsblk", "-nro", "PATH,MOUNTPOINT", self.dev],
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            path, _, mnt = line.partition(" ")
            if mnt:
                run(self.log, ["umount", path])

        dd = f"dd of={shlex.quote(self.dev)} bs={server.DD_BS} oflag=direct conv=fsync iflag=fullblock status=progress"
        if compressed:
            cmd = f"xz -dc -T0 {shlex.quote(src)} | {dd}"
        else:
            cmd = f"{dd} if={shlex.quote(src)}"
        self.log(f"Writing {src} -> {self.dev}")
        self.log("$ " + cmd)
        if subprocess.run(["bash", "-o", "pipefail", "-c", cmd]).returncode != 0:
            raise ProvisionError("writing the image failed")
        run(self.log, ["partprobe", self.dev], check=False)
        run(self.log, ["udevadm", "settle"], check=False)
        flush_card_cache(self.log, self.dev)

    # ------------------------------------------------ partition
    def step_partition(self):
        layout = self.cfg.get("layout")
        if not layout:
            self.log("No \"layout\" in config — keeping the image's partitions.")
            return
        table = json.loads(run(self.log, ["sfdisk", "--json", self.dev]).stdout)["partitiontable"]
        dev_sectors = server.device_size_bytes(self.dev) // SECTOR
        script = build_sfdisk_script(table, layout, dev_sectors)
        self.log("New partition table:\n" + script)
        run(self.log, ["sfdisk", "--wipe-partitions", "never", self.dev], input=script)
        run(self.log, ["partprobe", self.dev], check=False)
        run(self.log, ["udevadm", "settle"], check=False)
        flush_card_cache(self.log, self.dev)

        root = part_path(self.dev, 2)
        r = run(self.log, ["e2fsck", "-f", "-y", root], check=False)
        if "unsupported feature" in r.stdout + r.stderr:
            # The image's ext4 was made by a newer e2fsprogs than this PC has
            # (e.g. orphan_file vs Ubuntu 22.04's 1.46.5). The card's own
            # resize2fs grows it online once the VM is up (step_boot).
            self.log("This PC's e2fsprogs is too old for the card's root filesystem — "
                     "it will be grown inside the VM instead.")
        elif r.returncode not in (0, 1):  # 1 = errors corrected
            raise ProvisionError(f"e2fsck on {root} failed (exit {r.returncode})")
        else:
            run(self.log, ["resize2fs", root])

        # New partitions may sit over an old card's filesystems; clear them so
        # nothing (e.g. a desktop automounter) mistakes them for real data.
        new = [p["num"] for p in layout.get("partitions", [])]
        for n in new:
            run(self.log, ["wipefs", "-a", part_path(self.dev, n)], check=False)

    # ------------------------------------------------ inject
    def ensure_key(self):
        if not os.path.exists(self.key):
            os.makedirs(os.path.dirname(self.key), exist_ok=True)
            run(self.log, ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C",
                           "piforge-provision", "-f", self.key])
            chown_real(self.key)
            chown_real(self.key + ".pub")
        with open(self.key + ".pub") as f:
            return f.read().strip()

    def step_inject(self):
        card = dict(server.BUILTIN_DEFAULTS)
        card.update(self.cfg["card"])
        if not card.get("password"):
            raise ProvisionError("card.password is required")
        keys = [card.get("ssh_authorized_key", "").strip(), self.ensure_key()]
        card["ssh_authorized_key"] = "\n".join(k for k in keys if k)
        card["enable_ssh"] = True

        # Where the card's own systemd sees the boot partition: /boot/firmware
        # on Bookworm and later (/boot is then a plain folder on root), /boot before.
        with mounted(self.log, part_path(self.dev, 2), readonly=True) as root:
            fwloc = "/boot/firmware" if os.path.isdir(os.path.join(root, "boot", "firmware")) else "/boot"

        with mounted(self.log, part_path(self.dev, 1)) as boot:
            with open(os.path.join(boot, "firstrun.sh"), "w") as f:
                f.write(make_firstrun(card, self.hostname))
            cmdline_path = os.path.join(boot, "cmdline.txt")
            with open(cmdline_path) as f:
                cmdline = f.read().strip()
            if "\x00" in cmdline or "root=" not in cmdline:
                raise ProvisionError("cmdline.txt on the card looks damaged — retry from the flash step")
            # init=.../firstboot is Pi OS's auto-resize (and PARTUUID
            # re-randomise) on first boot — it must not grow root over the
            # partitions we just added.
            cmdline = re.sub(r"\s*init=\S*raspberrypi-sys-mods/firstboot", "", cmdline)
            cmdline = re.sub(r" systemd\.run.*", "", cmdline)
            cmdline += (f" systemd.run={fwloc}/firstrun.sh"
                        " systemd.run_success_action=reboot"
                        " systemd.unit=kernel-command-line.target\n")
            with open(cmdline_path, "w") as f:
                f.write(cmdline)
        self.log(f"Injected firstrun.sh ({fwloc}, hostname={self.hostname}, user={card['user']}) "
                 "and disabled auto-resize.")

    # ------------------------------------------------ VM
    def vm_append(self):
        """The card's own cmdline.txt, adapted for QEMU's virt board."""
        with mounted(self.log, part_path(self.dev, 1), readonly=True) as boot:
            with open(os.path.join(boot, "cmdline.txt")) as f:
                text = f.read()
        if "\x00" in text or "root=" not in text:
            raise ProvisionError("cmdline.txt on the card looks damaged — retry from the flash step")
        tokens = text.split()
        drop = ("console=", "init=")
        keep = [t for t in tokens
                if not t.startswith(drop) and t not in ("quiet", "splash", "plymouth.ignore-serial-consoles")]
        # With a screen, boot messages go to it too (tty1); the serial console
        # stays last, so it remains /dev/console and keeps the console log.
        consoles = ["console=tty1", "console=ttyAMA0"] if self.vm.get("screen") else ["console=ttyAMA0"]
        return " ".join(keep + consoles + ["net.ifnames=0"])

    def write_disk_alias(self):
        """In QEMU the card is /dev/vda; install scripts that hardcode the Pi's
        /dev/mmcblk0pN get matching symlinks. VM-only — removed in finish."""
        alias = self.vm.get("disk_alias")
        if not alias:
            return
        rule = ("# Written by PiForge provision.py for the QEMU stage only;\n"
                "# removed before the card leaves the VM.\n"
                f'KERNEL=="vda", SYMLINK+="{alias}"\n'
                f'KERNEL=="vda[0-9]*", SYMLINK+="{alias}p%n"\n')
        with mounted(self.log, part_path(self.dev, 2)) as root:
            path = root + UDEV_RULE
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(rule)

    def qemu_cmd(self, append):
        kernel = resolve_kernel(self.cfg) or expand(self.vm["kernel"])
        port = int(self.vm["ssh_port"])
        cmd = [
            "qemu-system-aarch64", "-M", "virt", "-cpu", "max",
            "-accel", "tcg,thread=multi",
            "-smp", str(self.vm["cpus"]), "-m", str(self.vm["memory"]),
            "-kernel", kernel, "-append", append,
            "-drive", f"file={self.dev},format=raw,if=none,id=card,cache=writeback",
            "-device", "virtio-blk-pci,drive=card",
            "-netdev", f"user,id=net0,hostfwd=tcp:127.0.0.1:{port}-:22",
            "-device", "virtio-net-pci,netdev=net0",
            "-device", "virtio-rng-pci",
            "-monitor", "none",
            "-serial", f"file:{os.path.join(self.log_dir, 'console-%d.log' % int(time.time()))}",
            # A guest reboot ends QEMU instead: the first boot's firstrun.sh
            # reboots once, and the next boot needs the new cmdline.txt.
            "-no-reboot",
        ]
        if self.vm.get("screen"):
            # VNC on localhost only; its WebSocket is what the app's Screen window
            # (noVNC) connects to. Ports follow the SSH port, so cards running
            # side by side get their own.
            ws = self.screen_ws_port()
            cmd += ["-device", "virtio-gpu-pci", "-device", "virtio-keyboard-pci", "-device", "virtio-tablet-pci",
                    "-vnc", f"127.0.0.1:{port - 2222 + 50},websocket=127.0.0.1:{ws}"]
        else:
            cmd += ["-display", "none"]
        for s in self.cfg["shares"]:
            host = expand(s["host_path"])
            if not os.path.isdir(host):
                raise ProvisionError(f"share host_path does not exist: {host}")
            cmd += ["-virtfs", f"local,path={host},mount_tag={s['tag']},security_model=none,readonly=on"]
        return cmd

    def screen_ws_port(self):
        return int(self.vm["ssh_port"]) + 3000

    def start_qemu(self, append):
        kernel = resolve_kernel(self.cfg)
        if not kernel:
            raise ProvisionError(f"VM kernel not found: {expand(self.vm['kernel'])} — build it with "
                                 "vm-kernel/build-vm-kernel.sh (or the app's Build VM kernel button)")
        cmd = self.qemu_cmd(append)
        self.log("$ " + " ".join(shlex.quote(c) for c in cmd))
        self.qemu = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, start_new_session=True)
        if self.vm.get("screen"):
            self.log(f"Screen: ws://127.0.0.1:{self.screen_ws_port()}")

    def wait_qemu_exit(self, timeout):
        try:
            self.qemu.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def step_boot(self):
        self.write_disk_alias()
        append = self.vm_append()
        if "systemd.run=" in append:
            self.log("First boot in the VM (applies firstrun.sh, then reboots — slow under emulation)...")
            self.start_qemu(append)
            if not self.wait_qemu_exit(int(self.vm["firstrun_timeout_min"]) * 60):
                self.qemu.kill()
                raise ProvisionError("first boot did not finish in time — see console-*.log")
            append = self.vm_append()
            if "systemd.run=" in append:
                raise ProvisionError("firstrun.sh did not complete (cmdline.txt still has systemd.run) — see console-*.log")
        self.log("Booting the VM...")
        self.start_qemu(append)
        self.wait_ssh()
        if self.cfg.get("layout"):
            # Grow root to fill its partition when step_partition couldn't
            # (a no-op "Nothing to do!" when it already did).
            self.ensure_sudo()
            self.sudo('resize2fs "$(findmnt -no SOURCE /)"')

    def ssh_base(self):
        return ["ssh", "-i", self.key, "-p", str(self.vm["ssh_port"]),
                "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
                "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=30",
                "-o", "ServerAliveCountMax=10", f"{self.user}@127.0.0.1"]

    def ssh(self, remote, check=True, input=None, quiet=False):
        if not quiet:
            self.log(f"[vm] $ {remote}")
        r = subprocess.run(self.ssh_base() + [remote], text=True, capture_output=True, input=input)
        if not quiet:
            for stream in (r.stdout, r.stderr):
                if stream and stream.strip():
                    self.log.raw(stream if stream.endswith("\n") else stream + "\n")
        if check and r.returncode != 0:
            raise ProvisionError(f"in VM, exit {r.returncode}: {remote}")
        return r

    def ssh_stream(self, remote):
        """Long-running command (the install): output goes live to the
        terminal and the log."""
        self.log(f"[vm] $ {remote}")
        proc = subprocess.Popen(self.ssh_base() + [remote], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:
            self.log.raw(line)
        return proc.wait()

    def wait_ssh(self):
        deadline = time.time() + int(self.vm["ssh_timeout_min"]) * 60
        while time.time() < deadline:
            if self.qemu.poll() is not None:
                raise ProvisionError(f"QEMU exited (code {self.qemu.returncode}) before SSH came up — see console-*.log")
            if self.ssh("true", check=False, quiet=True).returncode == 0:
                self.log(f"SSH is up ({self.user}@127.0.0.1:{self.vm['ssh_port']}).")
                return
            time.sleep(10)
        raise ProvisionError("timed out waiting for SSH in the VM")

    def ensure_vm(self):
        if self.qemu is None or self.qemu.poll() is not None:
            self.step_boot()

    def sudo(self, cmd, **kw):
        return self.ssh("sudo bash -c " + shlex.quote(cmd), **kw)

    def ensure_sudo(self):
        if self.ssh("sudo -n true", check=False, quiet=True).returncode == 0:
            return
        # No passwordless sudo for this user: add a temporary rule, removed in finish.
        self.log("Adding a temporary passwordless-sudo rule for provisioning...")
        rule = f"{self.user} ALL=(ALL) NOPASSWD: ALL"
        pw = self.cfg["card"].get("password", "")
        self.ssh(f"sudo -S -p '' bash -c {shlex.quote(f'echo {shlex.quote(rule)} > {TEMP_SUDOERS} && chmod 440 {TEMP_SUDOERS}')}",
                 input=pw + "\n", quiet=True)

    # ------------------------------------------------ install
    def mount_shares(self):
        for s in self.cfg["shares"]:
            g = shlex.quote(s["guest_path"])
            self.sudo(f"mkdir -p {g} && (mountpoint -q {g} || mount -t 9p -o "
                      f"trans=virtio,version=9p2000.L,msize=524288,ro {shlex.quote(s['tag'])} {g})")

    def copy_package(self):
        pkg = self.cfg.get("package")
        if not pkg:
            return
        dest = shlex.quote(pkg["guest_dir"])
        self.sudo(f"rm -rf {dest} && mkdir -p {dest}")
        if pkg.get("local_dir"):
            src = expand(pkg["local_dir"])
            include = pkg.get("include") or ["."]
            self.log(f"Copying package {src} ({', '.join(include)}) -> VM:{pkg['guest_dir']}")
            tar = subprocess.Popen(["tar", "-C", src, "-czf", "-", *include], stdout=subprocess.PIPE)
            r = subprocess.run(self.ssh_base() + [f"sudo tar -xzf - -C {dest}"], stdin=tar.stdout)
            tar.stdout.close()
            if tar.wait() != 0 or r.returncode != 0:
                raise ProvisionError("copying the install package into the VM failed")
        elif pkg.get("zip_url"):
            self.sudo("command -v unzip >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y unzip")
            self.sudo(f"curl -fsSL {shlex.quote(pkg['zip_url'])} -o /tmp/piforge-pkg.zip && "
                      f"unzip -q -o /tmp/piforge-pkg.zip -d {dest} && rm -f /tmp/piforge-pkg.zip")
        else:
            raise ProvisionError("package needs local_dir or zip_url")

    def step_install(self):
        self.ensure_vm()
        self.ensure_sudo()
        self.mount_shares()
        self.copy_package()
        if not self.cfg["install_cmd"]:
            self.log("No install_cmd — skipping.")
            return
        self.sudo(f"mkdir -p {os.path.dirname(INITRAMFS_CONF)} && "
                  f"echo 'MODULES=most  # PiForge VM stage only' > {INITRAMFS_CONF}")
        env = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in self.cfg["install_env"].items())
        remote = f"sudo env {env} bash -c {shlex.quote(self.cfg['install_cmd'])}"
        self.log("Running the install inside the VM (this is the long part)...")
        rc = self.ssh_stream(remote)
        if rc != 0:
            raise ProvisionError(f"install_cmd failed (exit {rc}) — see provision.log")

    # ------------------------------------------------ tailscale
    def step_tailscale(self):
        ts = self.cfg.get("tailscale")
        if not ts:
            self.log("No \"tailscale\" in config — skipping.")
            return
        key = ts.get("auth_key", "")
        if server._is_placeholder(key):
            # The card's own first-boot scripts can join Tailscale on the Pi.
            self.log("No Tailscale auth key — skipping (the card joins Tailscale on the Pi instead).")
            return
        self.ensure_vm()
        self.ensure_sudo()
        if self.ssh("command -v tailscale", check=False, quiet=True).returncode != 0:
            self.sudo("curl -fsSL https://tailscale.com/install.sh | sh")
        self.sudo("systemctl enable --now tailscaled")
        # Key via stdin into a root-only file, not on a command line.
        self.ssh("sudo install -m 600 /dev/stdin /run/piforge-tskey", input=key + "\n", quiet=True)
        args = " ".join(shlex.quote(a) for a in ts.get("extra_args", []))
        try:
            self.sudo(f"tailscale up --auth-key=file:/run/piforge-tskey "
                      f"--hostname={shlex.quote(self.hostname)} {args}")
        finally:
            self.sudo("rm -f /run/piforge-tskey", check=False)
        self.tailscale_ip = self.sudo("tailscale ip -4").stdout.strip().splitlines()[0]
        self.log(f"Tailscale IP: {self.tailscale_ip}")

    # ------------------------------------------------ verify
    def step_verify(self):
        self.ensure_vm()
        self.ensure_sudo()
        # Every image in the tar folder must have been loaded into the card.
        image_checks = [f"docker image inspect {shlex.quote(tag)} > /dev/null"
                        for tag in server.share_image_tags(self.cfg)]
        for check in image_checks + list(self.cfg["verify_cmds"]):
            ok = self.sudo(check, check=False).returncode == 0
            self.log(("  PASS  " if ok else "  FAIL  ") + check)
            if not ok:
                self.failed_checks.append(check)
        if self.failed_checks:
            raise ProvisionError(f"{len(self.failed_checks)} verify check(s) failed")

    # ------------------------------------------------ finish
    def step_finish(self, offline_cleanup=False):
        """offline_cleanup: also clean the card from the host side, for a VM
        that crashed or a run resumed with --from finish."""
        if self.qemu is not None and self.qemu.poll() is None:
            self.log("Cleaning up VM-only files and powering off...")
            cleanup = [f"rm -f {UDEV_RULE} {INITRAMFS_CONF}"]
            for s in self.cfg["shares"]:
                cleanup.append(f"umount {shlex.quote(s['guest_path'])} 2>/dev/null")
            pkg = self.cfg.get("package")
            if pkg:
                cleanup.append(f"rm -rf {shlex.quote(pkg['guest_dir'])}")
            if not self.vm.get("keep_provision_key"):
                cleanup.append(f"sed -i '/ piforge-provision$/d' /home/{self.user}/.ssh/authorized_keys")
            cleanup.append(f"rm -f {TEMP_SUDOERS}")
            # One root shell, poweroff queued last: once the temporary sudoers
            # rule is gone, a second `sudo` could no longer run.
            self.sudo("; ".join(cleanup) + "; sync; systemctl --no-block poweroff", check=False)
            if not self.wait_qemu_exit(600):
                self.log("WARNING: VM did not power off within 10 min — killing QEMU.")
                self.qemu.kill()
                self.qemu.wait()
        elif offline_cleanup or self.qemu is not None:
            self.clean_card_offline()
        run(self.log, ["sync"])
        run(self.log, ["blockdev", "--flushbufs", self.dev], check=False)

    def clean_card_offline(self):
        self.log("Removing VM-only files from the card (VM not running)...")
        with mounted(self.log, part_path(self.dev, 2)) as root:
            for path in (UDEV_RULE, TEMP_SUDOERS, INITRAMFS_CONF):
                if os.path.exists(root + path):
                    os.remove(root + path)
            ak = f"{root}/home/{self.user}/.ssh/authorized_keys"
            if not self.vm.get("keep_provision_key") and os.path.exists(ak):
                with open(ak) as f:
                    lines = f.readlines()
                with open(ak, "w") as f:
                    f.writelines(l for l in lines if not l.rstrip().endswith(" piforge-provision"))


# ---------------------------------------------------------------- main

def record(dev, hostname, ip, result, detail, log_dir):
    os.makedirs(server.IMAGES_DIR, exist_ok=True)
    new = not os.path.exists(REGISTRY_PATH)
    with open(REGISTRY_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REGISTRY_FIELDS)
        if new:
            w.writeheader()
        w.writerow({"timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
                    "device": dev, "hostname": hostname, "tailscale_ip": ip,
                    "result": result, "detail": detail[:300], "log_dir": log_dir})
    chown_real(REGISTRY_PATH)


def preflight(steps):
    tools = ["sfdisk", "e2fsck", "resize2fs", "wipefs", "partprobe", "blockdev",
             "dd", "xz", "ssh", "ssh-keygen", "mount", "tar", "lsblk"]
    if VM_STEPS & set(steps):
        tools.append("qemu-system-aarch64")
    missing = [t for t in tools if not any(
        os.access(os.path.join(d, t), os.X_OK) for d in os.environ.get("PATH", "").split(":") + ["/sbin", "/usr/sbin"])]
    if missing:
        raise ProvisionError("missing tools: " + ", ".join(missing) +
                             " — see README 'Provisioning in a VM' for the apt packages")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("device", help="the SD card, e.g. /dev/sdb")
    ap.add_argument("--config", required=True, help="provision JSON (see provision.example.json)")
    ap.add_argument("--hostname", required=True, help="this card's hostname (also its Tailscale name)")
    ap.add_argument("--from", dest="start", choices=STEPS, default="flash",
                    help="resume from this step (default: flash)")
    ap.add_argument("--yes", action="store_true", help="don't ask before erasing the card")
    ap.add_argument("--ssh-port", type=int, help="override vm.ssh_port (to run several cards at once)")
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("ERROR: run with sudo — this writes the card and runs QEMU on it.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}", args.hostname):
        sys.exit(f"ERROR: invalid hostname {args.hostname!r}")

    cfg = load_config(args.config)
    if args.ssh_port:
        cfg["vm"]["ssh_port"] = args.ssh_port
    dev = os.path.realpath(args.device)
    allowed = {d["device"]: d for d in server.list_devices()}
    if dev not in allowed:
        sys.exit(f"ERROR: {dev} is not a removable card PiForge will touch "
                 f"(candidates: {', '.join(allowed) or 'none'})")
    steps = STEPS[STEPS.index(args.start):]
    if {"flash", "partition"} & set(steps) and not args.yes:
        d = allowed[dev]
        print(f"About to ERASE {dev} — {d['model']} {d['size_h']}"
              + (f" (has data: {', '.join(d['fstypes'])})" if d["has_data"] else ""))
        if input("Type 'yes' to continue: ").strip() != "yes":
            sys.exit("Aborted.")

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_dir = os.path.join(server.IMAGES_DIR, "provision", f"{args.hostname}-{stamp}")
    os.makedirs(log_dir, exist_ok=True)
    for p in (server.IMAGES_DIR, os.path.dirname(log_dir), log_dir):
        chown_real(p)
    log = Log(os.path.join(log_dir, "provision.log"))
    log(f"Provisioning {dev} as {args.hostname} — steps: {', '.join(steps)}")
    log(f"Logs: {log_dir}")

    p = Provisioner(cfg, dev, args.hostname, log, log_dir)
    result, detail = "done", ""
    t0 = time.time()
    try:
        preflight(steps)
        if steps[0] not in ("flash", "partition", "inject"):
            p.ensure_key()
        for step in steps:
            if step == "finish":
                break
            log(f"==== {step} ====")
            getattr(p, f"step_{step}")()
    except (ProvisionError, subprocess.SubprocessError, OSError) as e:
        result, detail = "error", str(e)
        log(f"ERROR: {e}")
    except KeyboardInterrupt:
        result, detail = "cancelled", "interrupted"
        log("Interrupted.")
    finally:
        # Always power the VM off cleanly — pulling a card out from under a
        # running VM corrupts it.
        try:
            log("==== finish ====")
            p.step_finish(offline_cleanup=(args.start == "finish"))
        except Exception as e:
            log(f"ERROR during finish: {e}")
            if result == "done":
                result, detail = "error", f"finish: {e}"
        if p.qemu is not None and p.qemu.poll() is None:
            p.qemu.kill()

    if p.failed_checks:
        detail = "failed checks: " + " | ".join(p.failed_checks)
    record(dev, args.hostname, p.tailscale_ip, result, detail, log_dir)
    log(f"Result: {result.upper()} in {server.format_duration(time.time() - t0)}"
        + (f" — Tailscale IP {p.tailscale_ip}" if p.tailscale_ip else "")
        + (f" — {detail}" if detail and result != "done" else ""))
    if result == "done":
        log("Safe to remove the card.")
    log(f"Registry: {REGISTRY_PATH}")
    sys.exit(0 if result == "done" else 1)


if __name__ == "__main__":
    main()
