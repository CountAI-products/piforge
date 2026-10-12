#!/usr/bin/env python3
"""
clone.py — save a finished SD card as an image, and write exact copies of it.

  read   master card -> image file (partition table, FAT partitions in full,
         ext4 partitions as their USED blocks only via `e2image -ra`, so the
         image is about the size of the data, not the card)
  write  image -> one card: same partition table (same disk ID, so PARTUUIDs
         in cmdline.txt / fstab stay valid), same data

Requires e2fsprogs >= 1.47 for filesystem verification. The last ext4
partition is shrunk when saving and grown to fill the target when writing.
Production copies require --hostname. Tailscale enrollment is performed
separately with the existing per-card provisioning workflow after cloning.

  sudo python3 clone.py read  /dev/sdb --name master-v1
  sudo python3 clone.py write /dev/sdc --name master-v1 --hostname mill-002
  python3 clone.py list

Images live in ~/rpi-images/clones/<name>.img (sparse) + <name>.json.
Output lines starting with "STEP " / "PROGRESS " are for the PiForge app.
"""
import blockmap
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import server  # device listing, image dir, chown helpers

from firstrun_gen import make_identity_firstrun

SECTOR = 512
MIB = 1 << 20
CLONE_DIR = os.path.join(server.IMAGES_DIR, "clones")


class CloneError(Exception):
    pass


def _chown_real(path):
    uid = os.environ.get("SUDO_UID") or os.environ.get("PKEXEC_UID")
    gid = os.environ.get("SUDO_GID")
    if uid and not gid:
        import pwd
        gid = str(pwd.getpwuid(int(uid)).pw_gid)
    if uid and gid:
        os.chown(path, int(uid), int(gid))


def say(msg):
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def step(name):
    print(f"STEP {name}", flush=True)


def run(cmd, check=True, **kw):
    say("$ " + " ".join(cmd))
    r = subprocess.run(cmd, text=True, capture_output=True, **kw)
    for stream in (r.stdout, r.stderr):
        if stream and stream.strip():
            print(stream.rstrip(), flush=True)
    if check and r.returncode != 0:
        raise CloneError(f"{cmd[0]} failed (exit {r.returncode})")
    return r


def e2fs_can_resize():
    """e2fsck/resize2fs >= 1.47 understand current Pi OS ext4 (orphan_file);
    older ones refuse to check or resize it."""
    out = subprocess.run(["e2fsck", "-V"], capture_output=True, text=True).stderr
    m = re.search(r"(\d+)\.(\d+)", out)
    return bool(m) and (int(m.group(1)), int(m.group(2))) >= (1, 47)


def image_paths(name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name or ""):
        raise CloneError("image name: letters, digits, '.', '_' and '-' only")
    base = os.path.join(CLONE_DIR, name)
    return base + ".img", base + ".json"


def list_images():
    out = []
    if os.path.isdir(CLONE_DIR):
        for f in sorted(os.listdir(CLONE_DIR)):
            if f.endswith(".json"):
                try:
                    with open(os.path.join(CLONE_DIR, f)) as fh:
                        meta = json.load(fh)
                    img = os.path.join(CLONE_DIR, f[:-5] + ".img")
                    meta["on_disk"] = os.stat(img).st_blocks * 512 if os.path.exists(img) else 0
                    out.append(meta)
                except (OSError, ValueError):
                    continue
    return out


def partition_table(path):
    """sfdisk's view: label, id and partitions (start/size in sectors)."""
    r = subprocess.run(["sfdisk", "--json", path], capture_output=True, text=True)
    if r.returncode != 0:
        raise CloneError(f"can't read the partition table of {path}: {r.stderr.strip()}")
    t = json.loads(r.stdout)["partitiontable"]
    if t.get("label") != "dos":
        raise CloneError(f"expected an MBR (dos) partition table, found {t.get('label')!r}")
    for p in t["partitions"]:
        p["num"] = int(re.search(r"(\d+)$", p["node"]).group(1))
    return t


def sfdisk_script(table, sizes=None):
    """A partition table as sfdisk input; `sizes` overrides {num: sectors}."""
    sizes = sizes or {}
    lines = ["label: dos", f"label-id: {table['id']}", "unit: sectors", ""]
    for p in sorted(table["partitions"], key=lambda p: p["num"]):
        line = f"{p['node']} : start={p['start']}, size={sizes.get(p['num'], p['size'])}, type={p['type']}"
        if p.get("bootable"):
            line += ", bootable"
        lines.append(line)
    return "\n".join(lines) + "\n"


def fs_type_at(path, offset):
    r = subprocess.run(["blkid", "-p", "-o", "value", "-s", "TYPE", "-O", str(offset), path],
                       capture_output=True, text=True)
    return r.stdout.strip()


def data_partitions(table):
    return [p for p in sorted(table["partitions"], key=lambda p: p["num"])
            if p["type"] not in ("5", "f", "85")]          # not extended containers


def validate_layout(parts):
    types = {p["num"]: p.get("fstype") for p in parts}
    if types.get(1) not in ("vfat", "fat", "fat32") or types.get(2) != "ext4":
        raise CloneError("Expected FAT boot partition 1 and ext4 root partition 2")


def copy_partition(src, src_off, dst, dst_off, size, fstype, label):
    """Copy one partition between two whole-disk paths at byte offsets: ext4 as
    its used blocks (`e2image -ra`, which writes every metadata block, so old
    data left on the target in free blocks is harmless), anything else in full."""
    print(f"COPY {label}", flush=True)
    if fstype == "ext4":
        say(f"{label}: copying used blocks (ext4)")
        command = ["e2image", "-ra", "-p", "-o", str(src_off), "-O", str(dst_off), src, dst]
    else:
        say(f"{label}: copying in full ({fstype or 'unknown filesystem'}, {size // MIB} MiB)")
        command = ["dd", f"if={src}", f"of={dst}", "bs=4M", "iflag=skip_bytes,count_bytes",
                   "oflag=seek_bytes", "conv=notrunc,fsync", f"skip={src_off}", f"seek={dst_off}",
                   f"count={size}", "status=progress"]
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    buffer = bytearray()
    while True:
        character = proc.stdout.read(1)
        if not character:
            break
        if character in (b"\r", b"\n"):
            output = buffer.decode(errors="replace").strip()
            percent = parse_copy_progress(output, fstype, size)
            if percent is not None:
                print(f"PROGRESS {label} {percent:.0f}", flush=True)
            elif output:
                print(output, flush=True)
            buffer.clear()
        else:
            buffer.extend(character)
    if buffer:
        print(buffer.decode(errors="replace"), flush=True)
    if proc.wait() != 0:
        raise CloneError(f"{command[0]} failed on {label}")
    print(f"PROGRESS {label} 100", flush=True)


def parse_copy_progress(output, fstype, size):
    if fstype == "ext4":
        match = re.search(r"(\d+(?:\.\d+)?)%", output)
        return min(100.0, float(match.group(1))) if match else None
    match = re.match(r"(\d+) bytes", output)
    return min(100.0, int(match.group(1)) * 100 / size) if match and size else None


def map_progress(label):
    last = [0.0]
    started = __import__('time').monotonic()
    def report(done, total):
        now = __import__('time').monotonic()
        if now-last[0] < 1 and done != total:
            return
        last[0] = now
        percent = done*100/total if total else 100
        print(f"PROGRESS {label} {min(99, int(percent))}", flush=True)
        elapsed = now-started
        speed = done/elapsed if elapsed > 0 else 0
        eta = (total-done)/speed if speed else -1
        print(f"METRICS {done} {total} {elapsed:.1f} {eta:.1f} {speed:.0f}", flush=True)
    return report


def unmount_all(dev):
    out = subprocess.run(["lsblk", "-nrpo", "PATH,MOUNTPOINT", dev], capture_output=True, text=True).stdout
    for line in out.splitlines():
        path, _, mnt = line.partition(" ")
        if mnt:
            run(["umount", path])


def check_card(dev):
    if dev not in {d["device"] for d in server.list_devices()}:
        raise CloneError(f"{dev} is not a removable card PiForge will touch")


def settle(dev):
    run(["partprobe", dev], check=False)
    run(["udevadm", "settle"], check=False)
    paths = subprocess.run(["lsblk", "-nlpo", "PATH", dev], capture_output=True, text=True).stdout.split()
    for path in paths or [dev]:
        run(["blockdev", "--flushbufs", path], check=False)


# ---------------------------------------------------------------- read
def read_master(dev, name):
    check_card(dev)
    if not e2fs_can_resize():
        raise CloneError("Cloning requires e2fsprogs 1.47+ for filesystem verification")
    img, meta_path = image_paths(name)
    if os.path.exists(img):
        raise CloneError(f"an image called {name!r} already exists — delete it or pick another name")
    os.makedirs(CLONE_DIR, exist_ok=True)
    _chown_real(CLONE_DIR)
    unmount_all(dev)
    settle(dev)

    step("table")
    table = partition_table(dev)
    parts = data_partitions(table)
    end = max(p["start"] + p["size"] for p in table["partitions"])
    for p in parts:
        p["fstype"] = fs_type_at(dev, p["start"] * SECTOR)
    say("Master: " + ", ".join(f"p{p['num']} {p['fstype'] or '?'} {p['size'] * SECTOR // MIB} MiB" for p in parts))

    tmp = img + ".part"
    with open(tmp, "wb") as f:
        f.truncate(end * SECTOR)                      # sparse until written

    validate_layout(parts)
    step("copy")
    for p in parts:
        copy_partition(dev, p["start"] * SECTOR, tmp, p["start"] * SECTOR, p["size"] * SECTOR,
                       p["fstype"], f"p{p['num']}")

    # Shrink the last partition to its data, so copies fit smaller cards and
    # writing grows it back to fill each one.
    last = max(parts, key=lambda p: p["start"])
    sizes = {}
    shrunk = False
    if last["fstype"] == "ext4" and e2fs_can_resize():
        step("shrink")
        part_file = img + ".lastpart"
        with open(part_file, "wb") as f:
            f.truncate(last["size"] * SECTOR)
        try:
            copy_partition(tmp, last["start"] * SECTOR, part_file, 0, last["size"] * SECTOR, "ext4", f"p{last['num']}")
            result = run(["e2fsck", "-fy", part_file], check=False)
            if result.returncode not in (0, 1):
                raise CloneError("Master filesystem failed its check")
            run(["resize2fs", "-M", part_file])
            out = run(["dumpe2fs", "-h", part_file]).stdout
            blocks = int(re.search(r"Block count:\s+(\d+)", out).group(1))
            bsize = int(re.search(r"Block size:\s+(\d+)", out).group(1))
            new_sectors = -(-blocks * bsize // MIB) * MIB // SECTOR + 2048     # + 1 MiB slack
            if new_sectors < last["size"]:
                sizes[last["num"]] = new_sectors
                ext = next((p for p in table["partitions"] if p["type"] in ("5", "f", "85")), None)
                if ext and ext["start"] < last["start"]:
                    sizes[ext["num"]] = last["start"] + new_sectors - ext["start"]
                # The shrunk filesystem over the old last partition's data.
                with open(tmp, "r+b") as f:
                    f.truncate(last["start"] * SECTOR)
                    f.truncate((last["start"] + new_sectors) * SECTOR)
                copy_partition(part_file, 0, tmp, last["start"] * SECTOR, new_sectors * SECTOR, "ext4", f"p{last['num']}")
                say(f"p{last['num']} shrunk from {last['size'] * SECTOR // MIB} MiB to {new_sectors * SECTOR // MIB} MiB")
                last["size"] = new_sectors
                end = last["start"] + new_sectors
                shrunk = True
        finally:
            os.remove(part_file)
    elif last["fstype"] == "ext4":
        say("This PC's e2fsprogs is older than 1.47: the last partition is kept at the master's size, "
            "so every copy needs a card at least as big as the master.")

    # The partition table last: e2image cuts a file target off at the end of
    # the filesystem it copied, which would take the logical partitions' EBRs
    # (inside the extended partition) with it.
    step("table")
    with open(tmp, "r+b") as f:
        f.truncate(end * SECTOR)
    run(["sfdisk", "--quiet", "--no-reread", "--no-tell-kernel", tmp], input=sfdisk_script(table, sizes))
    os.replace(tmp, img)
    meta = {
        "name": name, "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "source_device": dev, "source_bytes": server.device_size_bytes(dev),
        "image_bytes": end * SECTOR, "min_card_bytes": end * SECTOR,
        "grow_last": shrunk, "partitions": [{k: p[k] for k in ("num", "start", "size", "type", "fstype")} for p in parts],
    }
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    step("map")
    blockmap.ensure(img, meta_path, map_progress("map"))
    for path in (img, meta_path):
        _chown_real(path)
    say(f"Saved {img}: {end * SECTOR / (1 << 30):.1f} GiB image, "
        f"{os.stat(img).st_blocks * 512 / (1 << 30):.1f} GiB on disk")


# ---------------------------------------------------------------- write
def write_copy(dev, name, hostname=None):
    if not hostname:
        raise CloneError("A unique hostname is required for production clones")
    make_identity_firstrun(hostname)
    check_card(dev)
    if not e2fs_can_resize():
        raise CloneError("Cloning requires e2fsprogs 1.47+ for filesystem verification")
    img, meta_path = image_paths(name)
    with open(meta_path) as f:
        meta = json.load(f)
    validate_layout(meta["partitions"])
    card_bytes = server.device_size_bytes(dev)
    if card_bytes < meta["min_card_bytes"]:
        raise CloneError(f"{dev} is {card_bytes / (1 << 30):.1f} GiB — this image needs at least "
                         f"{meta['min_card_bytes'] / (1 << 30):.1f} GiB")
    if meta.get("grow_last") and not e2fs_can_resize():
        raise CloneError("this image's last partition was shrunk when it was saved, and this PC's e2fsprogs "
                         "(older than 1.47) can't grow it back — the copy would have a full /home. "
                         "Write it from a PC with Ubuntu 24.04 or newer.")
    step("map")
    mapping = blockmap.ensure(img, meta_path, map_progress("map"))
    unmount_all(dev)

    step("table")
    table = partition_table(img)
    parts = data_partitions(table)
    sizes = {}
    grow = bool(meta.get("grow_last"))
    last = max(parts, key=lambda p: p["start"])
    if grow:
        card_sectors = card_bytes // SECTOR
        sizes[last["num"]] = card_sectors - last["start"]
        ext = next((p for p in table["partitions"] if p["type"] in ("5", "f", "85")), None)
        if ext and ext["start"] < last["start"]:
            sizes[ext["num"]] = card_sectors - ext["start"]
    # --wipe always: clear the old card's filesystem signatures.
    run(["sfdisk", "--quiet", "--wipe", "always", "--wipe-partitions", "always", dev], input=sfdisk_script(table, sizes))
    settle(dev)

    step("copy")
    file_size = os.path.getsize(dev) if os.path.isfile(dev) else None   # an image file as the target
    fstypes = {p["num"]: p["fstype"] for p in meta["partitions"]}
    print("COPY image", flush=True)
    say("Writing reusable block map; no source filesystem scan")
    blockmap.copy(img, dev, mapping, map_progress("image"))
    if file_size:   # e2image cuts a file target off at the end of what it copied
        with open(dev, "r+b") as f:
            f.truncate(file_size)
    # Again after the data, so nothing written above can have touched the
    # logical partitions' EBRs (see read_master).
    run(["sfdisk", "--quiet", "--no-reread", dev], input=sfdisk_script(table, sizes))
    print("PROGRESS image 100", flush=True)
    run(["sync"])
    settle(dev)

    can_check = e2fs_can_resize()
    if grow:
        step("grow")
        lp = part_path(dev, last["num"])
        result = run(["e2fsck", "-fy", lp], check=False)
        if result.returncode not in (0, 1):
            raise CloneError("Cannot grow a filesystem that failed its check")
        run(["resize2fs", lp])
    step("verify")
    if can_check:
        for p in parts:
            if fstypes.get(p["num"]) == "ext4":
                r = run(["e2fsck", "-fn", part_path(dev, p["num"])], check=False)
                if r.returncode != 0:
                    raise CloneError(f"filesystem check failed on p{p['num']} of the copy")
        say("Every ext4 partition on the copy checks clean.")
    else:
        say("Filesystem check skipped (e2fsprogs older than 1.47 can't check current Pi OS ext4).")
    if hostname:
        step("identity")
        give_identity(dev, hostname)
    run(["sync"])
    settle(dev)
    server.log_history(dev, hostname or "", "cloned", 0, f"master:{name}")
    say(f"Copy of {name!r} written to {dev}{f' as {hostname}' if hostname else ''} — safe to remove.")


@contextmanager
def mounted(part, readonly=False):
    subprocess.run(["umount", part], capture_output=True)  # desktop automount
    mnt = tempfile.mkdtemp(prefix="piforge-")
    try:
        run(["mount", "-o", "ro" if readonly else "rw", part, mnt])
        yield mnt
    finally:
        subprocess.run(["sync"])
        subprocess.run(["umount", mnt], capture_output=True)
        os.rmdir(mnt)


def give_identity(dev, hostname):
    """Prepare unique identity offline and install early first-boot setup."""
    script = make_identity_firstrun(hostname)
    with mounted(part_path(dev, 2)) as root:
        fwloc = "/boot/firmware" if os.path.isdir(os.path.join(root, "boot", "firmware")) else "/boot"
        # Clear copied enrollment material BEFORE network services can start.
        import shutil
        shutil.rmtree(os.path.join(root, "etc/pi-provision"), ignore_errors=True)
        for rel in ("var/lib/tailscale/tailscaled.state", "etc/machine-id",
                    "var/lib/dbus/machine-id", "etc/systemd/system/tailscale-provision.service",
                    "etc/systemd/system/multi-user.target.wants/tailscale-provision.service",
                    "usr/local/sbin/firstboot-tailscale.sh"):
            path = os.path.join(root, rel)
            if os.path.lexists(path):
                os.unlink(path)
        for path in Path(root, "etc/ssh").glob("ssh_host_*"):
            path.unlink()
        Path(root, "etc/machine-id").write_text("")
    # Application data may be on a separate persistent partition.
    table = partition_table(dev)
    for partition in data_partitions(table):
        if partition["type"] == "83":
            with mounted(part_path(dev, partition["num"])) as partition_root:
                for rel in ("home/kniti/.post_install_done", ".post_install_done"):
                    marker = Path(partition_root, rel)
                    if marker.is_file():
                        marker.unlink()
    with mounted(part_path(dev, 1)) as boot:
        cmdline_path = os.path.join(boot, "cmdline.txt")
        with open(cmdline_path) as f:
            cmdline = f.read().strip()
        if "root=" not in cmdline:
            raise CloneError("cmdline.txt on the copy has no root= — is partition 1 the boot partition?")
        with open(os.path.join(boot, "firstrun.sh"), "w") as f:
            f.write(script)
        cmdline = re.sub(r" systemd\.run.*", "", cmdline)
        cmdline += (f" systemd.run={fwloc}/firstrun.sh"
                    " systemd.run_success_action=reboot"
                    " systemd.unit=kernel-command-line.target\n")
        with open(cmdline_path, "w") as f:
            f.write(cmdline)
    say(f"Identity prepared for {hostname}; provision its own Tailscale key after cloning.")


def part_path(dev, n):
    return f"{dev}p{n}" if dev[-1].isdigit() else f"{dev}{n}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("read", help="save a master card as an image")
    r.add_argument("device"); r.add_argument("--name", required=True)
    w = sub.add_parser("write", help="write an image to a card")
    w.add_argument("device"); w.add_argument("--name", required=True)
    w.add_argument("--hostname", help="give the copy its own identity (hostname, machine-id, SSH host keys, "
                                      "Tailscale) on first boot; without it the copy is exact")
    sub.add_parser("list", help="list saved images")
    args = ap.parse_args()
    if args.cmd == "list":
        for m in list_images():
            print(f"{m['name']}: {m['image_bytes'] / (1 << 30):.1f} GiB image, needs a card >= "
                  f"{m['min_card_bytes'] / (1 << 30):.1f} GiB, saved {m['created']}")
        return
    if os.geteuid() != 0:
        sys.exit("Run as root (sudo).")
    if getattr(args, "hostname", None) and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}", args.hostname):
        sys.exit(f"invalid hostname {args.hostname!r}")
    try:
        if args.cmd == "read":
            read_master(args.device, args.name)
        else:
            write_copy(args.device, args.name, args.hostname)
    except CloneError as e:
        say(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
