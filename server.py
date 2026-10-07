#!/usr/bin/env python3
"""
PiForge — mass SD-card installer for Raspberry Pi OS.

Serves index.html, auto-detects removable SD cards, decompresses the OS
image once and writes every selected card in parallel straight from the
page cache (dd), then injects Imager-style firstrun.sh customization
(hostname, user, password, SSH keys, Wi-Fi, country, timezone, keyboard,
static IP). Also serves named config profiles and a flash history log.

Run:  sudo python3 server.py       then open http://127.0.0.1:47823
"""

import collections
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import csv

from firstrun_gen import sh_hash_password, wifi_psk, make_firstrun, compute_static_ip

PORT = 47823
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_IMAGE_URL = "https://downloads.raspberrypi.com/raspios_oldstable_arm64_latest"


def real_home():
    """The invoking user's actual home directory, even when this process
    is running as root via sudo/pkexec (as it must be, to write block
    devices). Without this, '~' resolves to /root and every user's image
    cache, profiles, and history end up hidden in root's home instead of
    their own — fine for a single developer running this from a repo
    checkout, wrong for a packaged app meant to be installed and used by
    anyone. Falls back to '~' for a real root login or a plain dev-mode
    `python3 server.py` with no privilege escalation involved."""
    uid = os.environ.get("PKEXEC_UID")
    if uid:
        import pwd
        return pwd.getpwuid(int(uid)).pw_dir
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        import pwd
        return pwd.getpwnam(sudo_user).pw_dir
    return os.path.expanduser("~")


USER_CONFIG_DIR = os.path.join(real_home(), ".config", "piforge")


def resolve_data_path(filename):
    """Prefer a file already sitting next to server.py — the dev-repo
    workflow (clone + edit config.json in place), unchanged from before
    packaging existed. Otherwise use the invoking user's own config
    directory, which is what a real `apt install`'d copy under /opt should
    use instead of writing into its own (root-owned) install directory."""
    local = os.path.join(BASE_DIR, filename)
    if os.path.exists(local):
        return local
    os.makedirs(USER_CONFIG_DIR, exist_ok=True)
    return os.path.join(USER_CONFIG_DIR, filename)


CONFIG_PATH = resolve_data_path("config.json")
CONFIG_EXAMPLE_PATH = os.path.join(BASE_DIR, "config.example.json")
PROFILES_PATH = resolve_data_path("profiles.json")

IMAGES_DIR = os.path.join(real_home(), "rpi-images")
IMAGE_FILE = os.path.join(IMAGES_DIR, "os-image.img.xz")   # compressed download
RAW_IMAGE = os.path.join(IMAGES_DIR, "os-image.img")       # decompressed once
URL_MARKER = os.path.join(IMAGES_DIR, "os-image.url")      # which URL is cached
HISTORY_PATH = os.path.join(IMAGES_DIR, "flash-history.csv")
HISTORY_FIELDS = ["timestamp", "device", "hostname", "status", "duration_s", "image_url"]
DD_BS = "8M"

# Generic, secret-free fallback used only if config.json is absent — the UI
# lets you edit every field before flashing regardless.
BUILTIN_DEFAULTS = {
    "image_url": DEFAULT_IMAGE_URL,
    "hostname": "raspberrypi",
    "number_hostnames": True,
    "user": "pi",
    "password": "changeme",
    "wifi_ssid": "",
    "wifi_password": "",
    "wifi_country": "US",
    "timezone": "UTC",
    "keymap": "us",
    "enable_ssh": True,
    "verify": False,
    "ssh_authorized_key": "",
    "disable_ssh_password": False,
    "static_ip_base": "",
    "static_ip_cidr": 24,
    "static_ip_gateway": "",
    "static_ip_dns": "",
    "static_ip_iface": "eth0",
    "notify_on_finish": False,
}


def load_config_defaults():
    """Merge config.json (gitignored, user's real values) over built-in
    generic defaults. Never raises — a missing/broken config.json just
    means the form starts from safe generic placeholders."""
    cfg = dict(BUILTIN_DEFAULTS)
    for path in (CONFIG_EXAMPLE_PATH, CONFIG_PATH):
        try:
            with open(path) as f:
                cfg.update(json.load(f))
        except FileNotFoundError:
            pass
        except Exception:
            pass  # malformed config.json shouldn't crash the server
    return cfg


def current_image_url():
    return load_config_defaults().get("image_url") or DEFAULT_IMAGE_URL


def load_profiles():
    """Named config presets, e.g. {"classroom-kit": {...}}. Gitignored —
    may contain real Wi-Fi passwords per profile."""
    try:
        with open(PROFILES_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def save_profiles(profiles):
    tmp = PROFILES_PATH + ".part"
    with open(tmp, "w") as f:
        json.dump(profiles, f, indent=2)
    os.replace(tmp, PROFILES_PATH)


def log_history(device, hostname, status, duration_s, image_url):
    os.makedirs(IMAGES_DIR, exist_ok=True)
    is_new = not os.path.exists(HISTORY_PATH)
    with open(HISTORY_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        if is_new:
            w.writeheader()
        w.writerow({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "device": device,
            "hostname": hostname,
            "status": status,
            "duration_s": round(duration_s, 1),
            "image_url": image_url,
        })


def read_history(limit=50):
    if not os.path.exists(HISTORY_PATH):
        return []
    with open(HISTORY_PATH, newline="") as f:
        rows = list(csv.DictReader(f))
    return list(reversed(rows))[:limit]

# ---------------------------------------------------------------- state

JOBS = {}          # device -> {state, percent, message, hostname}
JOBS_LOCK = threading.Lock()
PREP_LOCK = threading.Lock()
PREP_STATE = {"phase": "idle", "percent": 0, "error": None}  # idle|download|extract|ready
FLASH_ACTIVE = threading.Event()

RUNNING_PROCS = {}   # device -> Popen of the currently running dd/verify step
PROCS_LOCK = threading.Lock()
CANCEL_FLAGS = set()  # devices with a pending/handled cancel request
CANCEL_LOCK = threading.Lock()


def set_job(dev, **kw):
    with JOBS_LOCK:
        JOBS.setdefault(dev, {})
        JOBS[dev].update(kw)


BUSY_STATES = {"queued", "downloading", "writing", "verifying", "configuring"}


def register_proc(dev, proc):
    with PROCS_LOCK:
        RUNNING_PROCS[dev] = proc


def unregister_proc(dev):
    with PROCS_LOCK:
        RUNNING_PROCS.pop(dev, None)


def is_cancelled(dev):
    with CANCEL_LOCK:
        return dev in CANCEL_FLAGS


def clear_cancel(dev):
    with CANCEL_LOCK:
        CANCEL_FLAGS.discard(dev)


def request_cancel(devices):
    """devices: explicit list, or falsy to cancel every currently-busy card.
    Kills the running dd/verify process group immediately; flash_device
    notices via is_cancelled() and marks the card 'cancelled' instead of
    'error'. Cards still queued/downloading (no process yet) are caught by
    the same check before they start writing."""
    if not devices:
        with JOBS_LOCK:
            devices = [d for d, j in JOBS.items() if j.get("state") in BUSY_STATES]
    with CANCEL_LOCK:
        CANCEL_FLAGS.update(devices)
    for dev in devices:
        set_job(dev, state="cancelling", message="Cancelling…")
        with PROCS_LOCK:
            proc = RUNNING_PROCS.get(dev)
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    return devices


# ---------------------------------------------------------------- devices

def list_devices():
    """Removable block devices safe to flash."""
    out = subprocess.run(
        ["lsblk", "-J", "-b", "-o", "NAME,SIZE,MODEL,TRAN,RM,TYPE,MOUNTPOINTS,FSTYPE"],
        capture_output=True, text=True, check=True).stdout
    devs = []
    for d in json.loads(out).get("blockdevices", []):
        if d.get("type") != "disk" or not d.get("rm"):
            continue
        if not d.get("size"):
            continue  # empty reader slot
        mounts = []
        fstypes = []
        def collect(node):
            for m in node.get("mountpoints") or []:
                if m:
                    mounts.append(m)
            if node.get("fstype"):
                fstypes.append(node["fstype"])
            for c in node.get("children") or []:
                collect(c)
        collect(d)
        if any(m in ("/", "/boot", "/boot/firmware", "/home") or m.startswith("/usr")
               for m in mounts):
            continue  # never offer a system disk
        path = "/dev/" + d["name"]
        with JOBS_LOCK:
            job = dict(JOBS.get(path, {}))
        devs.append({
            "device": path,
            "size": d["size"],
            "size_h": human_size(d["size"]),
            "model": (d.get("model") or "").strip() or "Unknown",
            "tran": d.get("tran") or "",
            "has_data": bool(fstypes),
            "fstypes": sorted(set(fstypes)),
            "job": job,
        })
    return devs


def device_size_bytes(dev):
    out = subprocess.run(["blockdev", "--getsize64", dev],
                         capture_output=True, text=True, check=True).stdout
    return int(out.strip())


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


# ---------------------------------------------------------------- image

def prepare_image(image_url=None):
    """Download + decompress once; safe to call from many threads.

    Decompressing once (xz -T0, all cores) and dd-ing the raw image means the
    kernel page cache feeds every card from RAM — no per-card decompression.
    image_url comes from the flash request itself (the UI's image picker),
    falling back to config.json's default only if the caller doesn't have
    one. If it changed since the last run, the stale cache is wiped so
    switching OS images (e.g. Lite vs desktop) redownloads.
    """
    image_url = image_url or current_image_url()
    with PREP_LOCK:
        cached_url = None
        if os.path.exists(URL_MARKER):
            cached_url = open(URL_MARKER).read().strip()
        if cached_url != image_url:
            for f in (IMAGE_FILE, RAW_IMAGE, URL_MARKER):
                if os.path.exists(f):
                    os.remove(f)
        global _UNCOMP_SIZE
        _UNCOMP_SIZE = None

        if os.path.exists(RAW_IMAGE):
            PREP_STATE.update(phase="ready", percent=100)
            return
        os.makedirs(IMAGES_DIR, exist_ok=True)
        try:
            if not os.path.exists(IMAGE_FILE):
                PREP_STATE.update(phase="download", percent=0, error=None)
                tmp = IMAGE_FILE + ".part"
                req = urllib.request.Request(image_url, headers={"User-Agent": "piforge"})
                with urllib.request.urlopen(req) as resp, open(tmp, "wb") as f:
                    total = int(resp.headers.get("Content-Length") or 0)
                    got = 0
                    while True:
                        chunk = resp.read(1024 * 512)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        if total:
                            PREP_STATE["percent"] = round(got * 100 / total, 1)
                os.replace(tmp, IMAGE_FILE)

            PREP_STATE.update(phase="extract", percent=0, error=None)
            total = uncompressed_size()
            tmp = RAW_IMAGE + ".part"
            with open(tmp, "wb") as out:
                proc = subprocess.Popen(["xz", "-dc", "-T0", IMAGE_FILE], stdout=out)
                while proc.poll() is None:
                    time.sleep(1)
                    PREP_STATE["percent"] = round(
                        min(100.0, os.path.getsize(tmp) * 100 / total), 1)
                if proc.returncode != 0:
                    raise RuntimeError("xz extraction failed")
            os.replace(tmp, RAW_IMAGE)
            with open(URL_MARKER, "w") as f:
                f.write(image_url)
            # Pre-warm page cache so parallel dd readers hit RAM, not disk
            subprocess.run(f"cat '{RAW_IMAGE}' > /dev/null", shell=True)
            PREP_STATE.update(phase="ready", percent=100)
        except Exception as e:
            PREP_STATE.update(phase="idle", error=str(e))
            raise


_UNCOMP_SIZE = None

def uncompressed_size():
    global _UNCOMP_SIZE
    if _UNCOMP_SIZE:
        return _UNCOMP_SIZE
    out = subprocess.run(["xz", "--robot", "-l", IMAGE_FILE],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        f = line.split("\t")
        if f and f[0] == "totals":
            _UNCOMP_SIZE = int(f[4])
            return _UNCOMP_SIZE
    return os.path.getsize(IMAGE_FILE) * 2  # rough fallback


# ---------------------------------------------------------------- firstrun
#
# The actual firstrun.sh template lives in firstrun_gen.py — one shared
# implementation used by this server, inject-config.sh, and flash-all.sh.


# ---------------------------------------------------------------- flashing

def sectors(dev, index):
    """Read cumulative read(2)/write(6) sectors from /sys/block/X/stat."""
    try:
        with open(f"/sys/block/{os.path.basename(dev)}/stat") as f:
            return int(f.read().split()[index])
    except Exception:
        return 0


def format_duration(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


def watch_progress(dev, proc, total_bytes, state, stat_index):
    """Poll sysfs disk stats while a flash/verify process runs."""
    base = sectors(dev, stat_index)
    label = "Writing" if state == "writing" else "Verifying"
    start = time.time()
    while proc.poll() is None:
        time.sleep(1)
        done = (sectors(dev, stat_index) - base) * 512
        pct = min(100.0, done * 100 / total_bytes)
        elapsed = time.time() - start
        rate_mb_s = (done / 1048576) / elapsed if elapsed > 0 else 0
        eta = f" · ETA {format_duration((total_bytes - done) / (done / elapsed))}" \
            if done > 0 and elapsed > 0 else ""
        set_job(dev, state=state, percent=round(pct, 1),
                message=f"{label}… {pct:.0f}% · {rate_mb_s:.1f} MB/s{eta}")


def boot_partition(dev):
    for suffix in ("1", "p1"):
        if os.path.exists(dev + suffix):
            return dev + suffix
    return None


class Cancelled(Exception):
    """Raised inside flash_device when the user cancelled this card."""


def check_cancelled(dev):
    if is_cancelled(dev):
        clear_cancel(dev)
        raise Cancelled()


def flash_device(dev, cfg, hostname, static_ip=None):
    log = f"/tmp/flash-{os.path.basename(dev)}.log"
    start_time = time.time()
    image_url = cfg.get("image_url") or current_image_url()
    try:
        check_cancelled(dev)  # cancelled while still queued, before any work

        if not os.path.exists(RAW_IMAGE):
            set_job(dev, state="downloading", percent=0, message="Preparing image (once)…")
            prepare_image(image_url)

        check_cancelled(dev)
        total = os.path.getsize(RAW_IMAGE)

        # Refuse to silently truncate: a card smaller than the image would
        # write successfully but boot into a corrupt/incomplete filesystem.
        try:
            dev_size = device_size_bytes(dev)
        except Exception:
            dev_size = None
        if dev_size is not None and dev_size < total:
            raise RuntimeError(
                f"card too small: {human_size(dev_size)} available, "
                f"{human_size(total)} needed for this image")

        set_job(dev, state="writing", percent=0, message="Starting write…", hostname=hostname)

        subprocess.run(f"umount {dev}?* 2>/dev/null", shell=True)

        with open(log, "w") as lf:
            proc = subprocess.Popen(
                ["dd", f"if={RAW_IMAGE}", f"of={dev}", f"bs={DD_BS}",
                 "oflag=direct", "conv=fsync", "iflag=fullblock"],
                stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
            register_proc(dev, proc)
            try:
                watch_progress(dev, proc, total, "writing", 6)
                rc = proc.wait()
            finally:
                unregister_proc(dev)
        check_cancelled(dev)
        if rc != 0:
            tail = open(log).read()[-400:]
            raise RuntimeError(f"dd exited {rc}: …{tail}")

        if cfg.get("verify"):
            set_job(dev, state="verifying", percent=0, message="Verifying…")
            with open(log, "a") as lf:
                proc = subprocess.Popen(
                    f"dd if='{dev}' bs={DD_BS} iflag=direct,fullblock 2>>'{log}'"
                    f" | head -c {total} | cmp -s - '{RAW_IMAGE}'",
                    shell=True, stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
                register_proc(dev, proc)
                try:
                    watch_progress(dev, proc, total, "verifying", 2)
                    rc = proc.wait()
                finally:
                    unregister_proc(dev)
            check_cancelled(dev)
            if rc != 0:
                raise RuntimeError("verification FAILED — card data differs from image")

        check_cancelled(dev)
        set_job(dev, state="configuring", percent=100, message="Injecting configuration…")
        subprocess.run(["partprobe", dev], capture_output=True)
        time.sleep(2)
        part = boot_partition(dev)
        if not part:
            raise RuntimeError("boot partition not found after write")

        # kick out any desktop automount so we are the only writer
        subprocess.run(["umount", part], capture_output=True)

        mnt = tempfile.mkdtemp(prefix="bootfs-")
        try:
            subprocess.run(["mount", part, mnt], check=True, capture_output=True)
            with open(os.path.join(mnt, "firstrun.sh"), "w") as f:
                f.write(make_firstrun(cfg, hostname, static_ip=static_ip))
            os.chmod(os.path.join(mnt, "firstrun.sh"), 0o755)
            cmdline_path = os.path.join(mnt, "cmdline.txt")
            with open(cmdline_path) as f:
                cmdline = f.read()
            cmdline = re.sub(r" systemd\.run.*", "", cmdline).rstrip("\n")
            cmdline += (" systemd.run=/boot/firstrun.sh"
                        " systemd.run_success_action=reboot"
                        " systemd.unit=kernel-command-line.target\n")
            with open(cmdline_path, "w") as f:
                f.write(cmdline)
            subprocess.run(["sync"])
        finally:
            subprocess.run(["umount", mnt], capture_output=True)

        # verify config landed: fresh read-only mount, check both files
        try:
            subprocess.run(["mount", "-o", "ro", part, mnt], check=True, capture_output=True)
            ok = (os.path.getsize(os.path.join(mnt, "firstrun.sh")) > 0
                  and "systemd.run=/boot/firstrun.sh" in open(os.path.join(mnt, "cmdline.txt")).read())
            if not ok:
                raise RuntimeError("config verify failed: firstrun.sh/cmdline.txt not persisted")
        finally:
            subprocess.run(["umount", mnt], capture_output=True)
            os.rmdir(mnt)

        set_job(dev, state="done", percent=100,
                message=f"Done — {hostname} configured & verified, safe to remove")
        log_history(dev, hostname, "done", time.time() - start_time, image_url)
    except Cancelled:
        unregister_proc(dev)
        set_job(dev, state="cancelled", percent=0,
                message="Cancelled — card is incomplete, reflash before use")
        log_history(dev, hostname, "cancelled", time.time() - start_time, image_url)
    except Exception as e:
        unregister_proc(dev)
        set_job(dev, state="error", percent=0, message=str(e)[:300])
        log_history(dev, hostname, "error", time.time() - start_time, image_url)


def start_flash(devices, cfg):
    # Re-validate against current safe device list. Index cards by their
    # position in the *full* currently-connected list (not just the ones
    # being flashed this call) so retrying a single failed card reuses the
    # same hostname/static-IP it would have gotten in the original batch,
    # instead of renumbering it as "card 1".
    all_present = sorted(d["device"] for d in list_devices())
    bad = [d for d in devices if d not in all_present]
    if bad:
        raise ValueError(f"not a removable/safe device: {', '.join(bad)}")

    cfg["_hash"] = sh_hash_password(cfg["password"])
    if cfg.get("wifi_ssid"):
        cfg["_psk"] = wifi_psk(cfg["wifi_ssid"], cfg.get("wifi_password", ""))

    threads = []
    for dev in devices:
        i = all_present.index(dev) + 1
        hostname = (f"{cfg['hostname']}{i}" if cfg.get("number_hostnames", True)
                    else cfg["hostname"])
        static_ip = compute_static_ip(cfg["static_ip_base"], i) if cfg.get("static_ip_base") else None
        set_job(dev, state="queued", percent=0, message="Queued…", hostname=hostname)
        t = threading.Thread(target=flash_device, args=(dev, cfg, hostname, static_ip), daemon=True)
        threads.append(t)

    def runner():
        FLASH_ACTIVE.set()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        FLASH_ACTIVE.clear()

    threading.Thread(target=runner, daemon=True).start()


# ---------------------------------------------------------------- full provisioning (VM)
#
# The "Full install" screen. Each card runs provision.py as its own child
# process (flash, partition, boot in QEMU, install, Tailscale, verify); this
# only starts them, follows their output, and stops them. Settings live in
# provision.json next to config.json — same rules (repo checkout first,
# else ~/.config/piforge/), since it holds the card password and the
# Tailscale key.

PROVISION_PATH = resolve_data_path("provision.json")
PROVISION_EXAMPLE_PATH = os.path.join(BASE_DIR, "provision.example.json")
# Optional site preset shipped with a packaged build (packaging/build-deb.sh
# copies provision.defaults.json if you put one in the repo root): what a new
# station starts from, before anyone presses Save.
PROVISION_DEFAULTS_PATH = os.path.join(BASE_DIR, "provision.defaults.json")
PROVISION_REGISTRY = os.path.join(IMAGES_DIR, "provisioned-cards.csv")
PROVISION_STEPS = ["flash", "partition", "inject", "boot", "install", "tailscale", "verify", "finish"]
KERNEL_OUT_DIR = os.path.join(real_home(), ".cache", "piforge", "vm-kernel")

PROV_JOBS = {}      # device -> job dict (see start_provision)
PROV_LOCK = threading.Lock()
KERNEL_BUILD = {"state": "idle", "message": "", "lines": collections.deque(maxlen=400)}
LOG_PREFIX = re.compile(r"^\[\d\d:\d\d:\d\d\] ")


def load_provision_settings():
    for path in (PROVISION_PATH, PROVISION_DEFAULTS_PATH, PROVISION_EXAMPLE_PATH):
        try:
            with open(path) as f:
                return json.load(f)
        except FileNotFoundError:
            continue
    return {}


def save_provision_settings(cfg):
    tmp = PROVISION_PATH + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, PROVISION_PATH)
    _chown_real(PROVISION_PATH)


def _chown_real(path):
    uid = os.environ.get("PKEXEC_UID") or os.environ.get("SUDO_UID")
    if uid:
        import pwd
        pw = pwd.getpwuid(int(uid))
        try:
            os.chown(path, pw.pw_uid, pw.pw_gid)
        except OSError:
            pass


def _expand(path):
    if path.startswith("~/"):
        return os.path.join(real_home(), path[2:])
    return path


def provision_kernel_path(cfg):
    """Same lookup as provision.resolve_kernel()."""
    configured = _expand(((cfg.get("vm") or {}).get("kernel")) or "~/.cache/piforge/vm-kernel/Image")
    for path in (configured, os.path.join(BASE_DIR, "vm-kernel", "Image")):
        if os.path.exists(path):
            return path
    return None


def _is_placeholder(value):
    return not value or "REPLACE" in value or value in ("changeme", "CHANGE_ME")


def _planner():
    """provision.py, for its partition planner. It does `import server`; when
    this file runs as `python3 server.py` it is __main__, so register it as
    "server" first or that import would load a second copy of it."""
    sys.modules.setdefault("server", sys.modules[__name__])
    import provision
    return provision


# Stand-in for a freshly flashed Raspberry Pi OS card (512 MiB boot at 8 MiB,
# root right after it): enough to size a layout before the card is flashed.
_PIOS_TABLE = {"label": "dos", "id": "0x00000000", "partitions": [
    {"start": 16384, "size": 1048576, "type": "c"},
    {"start": 1064960, "size": 2048, "type": "83"}]}
_GIB = 1 << 30


def layout_min_bytes(layout):
    """Smallest card a partition layout fits on, worked out with provision.py's
    own planner (plus 1 GiB for a last "rest" partition). ValueError if the
    layout itself is invalid."""
    prov = _planner()

    def fits(sectors):
        try:
            prov.build_sfdisk_script(_PIOS_TABLE, layout, sectors)
            return True
        except prov.ProvisionError as e:
            if "does not fit" in str(e):
                return False
            raise ValueError(str(e))
        except (KeyError, TypeError, AttributeError, ValueError):
            raise ValueError('expected {"root_size": "50G", "partitions": [{"num": 3, "size": "30G"}, …]}')

    lo, hi = 0, 1 << 37  # sectors: 0 .. 64 TiB
    if not fits(hi):  # an invalid layout raises here with the reason
        raise ValueError("needs a card larger than 64 TiB")
    while hi - lo > 1:
        mid = (lo + hi) // 2
        lo, hi = (lo, mid) if fits(mid) else (mid, hi)
    parts = layout.get("partitions") or []
    # The planner only allows "rest" on the highest-numbered partition.
    rest = bool(parts) and str(max(parts, key=lambda p: p["num"])["size"]).lower() == "rest"
    return hi * 512 + (_GIB if rest else 0)


def provision_layouts(cfg):
    """Named partition layouts the operator picks from per card. Settings
    without "layouts" offer their single "layout" as "Default"."""
    layouts = cfg.get("layouts") or {}
    if not layouts and cfg.get("layout"):
        layouts = {"Default": cfg["layout"]}
    return layouts


def layout_choices(cfg):
    """For the UI: every layout with the smallest card it fits, smallest first."""
    out = []
    for name, layout in provision_layouts(cfg).items():
        try:
            out.append({"name": name, "layout": layout, "min_bytes": layout_min_bytes(layout)})
        except ValueError as e:
            out.append({"name": name, "layout": layout, "min_bytes": None, "error": str(e)})
    return sorted(out, key=lambda c: (c["min_bytes"] is None, c["min_bytes"] or 0, c["name"]))


def validate_layouts(cfg):
    layouts = cfg.get("layouts")
    if layouts is None:
        return
    if not isinstance(layouts, dict):
        raise ValueError('"layouts" must be an object: {"name": {"root_size": …, "partitions": […]}}')
    for name, layout in layouts.items():
        if not str(name).strip():
            raise ValueError("every layout needs a name")
        try:
            layout_min_bytes(layout)
        except ValueError as e:
            raise ValueError(f"layout {name!r}: {e}")


TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz")
_TAR_TAGS = {}  # (path, size, mtime) -> image tags, so a big tar is read once


def image_folder(cfg):
    """The host folder of Docker image tars shared into the VM (the share
    tagged "dockertars", else the first share), or None."""
    shares = cfg.get("shares") or []
    share = next((s for s in shares if s.get("tag") == "dockertars"), shares[0] if shares else None)
    return _expand(share["host_path"]) if share and share.get("host_path") else None


def tar_image_tags(path):
    """The image names:tags in a `docker save` tar (plain or gzipped), read
    from its manifest.json without unpacking the whole thing."""
    st = os.stat(path)
    key = (path, st.st_size, st.st_mtime)
    if key not in _TAR_TAGS:
        import tarfile
        tags = []
        try:
            with tarfile.open(path, "r|*") as tar:
                for member in tar:
                    if member.name.lstrip("./") == "manifest.json":
                        for entry in json.load(tar.extractfile(member)):
                            tags += entry.get("RepoTags") or []
                        break
        except (tarfile.TarError, OSError, ValueError, EOFError):
            tags = None  # not a readable docker image tar
        _TAR_TAGS[key] = tags
    return _TAR_TAGS[key]


def list_image_tars(cfg):
    folder = image_folder(cfg)
    out = []
    if folder and os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if name.endswith(TAR_SUFFIXES) and os.path.isfile(path):
                size = os.path.getsize(path)
                out.append({"name": name, "size": size, "size_h": human_size(size),
                            "images": tar_image_tags(path)})
    return {"folder": folder, "tars": out}


def share_image_tags(cfg):
    """Every image in the tar folder: what the VM is expected to have loaded."""
    return [t for tar in list_image_tars(cfg)["tars"] for t in (tar["images"] or [])]


def _tar_path(cfg, name):
    name = os.path.basename(name or "")
    if not name.endswith(TAR_SUFFIXES):
        raise ValueError("only .tar, .tar.gz or .tgz files (from `docker save`)")
    folder = image_folder(cfg)
    if not folder:
        raise ValueError("set the Docker image folder in Settings first")
    return folder, os.path.join(folder, name)


def save_image_tar(name, stream, length):
    """Store an uploaded image tar in the image folder (written as .part, then
    renamed once it's complete and has a docker manifest)."""
    folder, path = _tar_path(load_provision_settings(), name)
    os.makedirs(folder, exist_ok=True)
    _chown_real(folder)
    tmp = path + ".part"
    try:
        with open(tmp, "wb") as f:
            left = length
            while left > 0:
                chunk = stream.read(min(left, 1 << 20))
                if not chunk:
                    raise ValueError("upload cut off before the end")
                f.write(chunk)
                left -= len(chunk)
        if not tar_image_tags(tmp):
            raise ValueError(f"{os.path.basename(path)} is not a Docker image tar (no manifest.json with image tags)")
        os.replace(tmp, path)
        _chown_real(path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def delete_image_tar(name):
    _, path = _tar_path(load_provision_settings(), name)
    if not os.path.isfile(path):
        raise ValueError(f"{os.path.basename(path)} is not in the image folder")
    os.remove(path)


def write_job_config(cfg, dev, layout):
    """One card's settings for provision.py: the saved ones with the layout
    picked for that card. Holds the card password, so 0600 like provision.json."""
    job_cfg = {k: v for k, v in cfg.items() if k != "layouts"} | {"layout": layout}
    path = os.path.join(os.path.dirname(PROVISION_PATH), f"provision-{os.path.basename(dev)}.json")
    tmp = path + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(job_cfg, f, indent=2)
    os.replace(tmp, path)
    _chown_real(path)
    return path


def provision_checks(cfg):
    """Everything a station needs before START makes sense, as a list the UI
    shows with a tick or a cross."""
    checks = []

    def add(key, ok, label, detail=""):
        checks.append({"key": key, "ok": bool(ok), "label": label, "detail": detail})

    add("root", os.geteuid() == 0, "Running with root rights",
        "" if os.geteuid() == 0 else "restart the station program with sudo")
    qemu = shutil.which("qemu-system-aarch64")
    add("qemu", qemu, "QEMU (virtual Pi)", qemu or "sudo apt install qemu-system-arm")
    kernel = provision_kernel_path(cfg)
    add("kernel", kernel, "VM kernel", kernel or "not built yet — use Build VM kernel")
    for share in cfg.get("shares") or []:
        host = _expand(share.get("host_path", ""))
        tars = []
        for pattern in ("*.tar", "*.tar.gz", "*.tgz"):
            tars += glob.glob(os.path.join(host, pattern))
        add(f"share:{share.get('tag')}", os.path.isdir(host) and tars,
            f"Docker image files ({share.get('tag')})",
            f"{len(tars)} tar file(s) in {host}" if os.path.isdir(host) else f"folder missing: {host}")
    pkg = cfg.get("package") or {}
    if pkg.get("local_dir"):
        d = _expand(pkg["local_dir"])
        add("package", os.path.isdir(d), "Install scripts", d if os.path.isdir(d) else f"folder missing: {d}")
    else:
        add("package", pkg.get("zip_url"), "Install scripts", pkg.get("zip_url") or "not set")
    card = cfg.get("card") or {}
    add("password", not _is_placeholder(card.get("password", "")), "Card password set")
    # No Tailscale check: the key is optional (without it the tailscale step
    # is skipped and the card joins Tailscale on the Pi instead).
    return checks


def read_provision_registry(limit=200):
    try:
        with open(PROVISION_REGISTRY, newline="") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        return []
    return rows[-limit:][::-1]


def next_hostname(cfg):
    """prefix + the next number after the highest one already used, counting
    finished cards and ones still running."""
    prefix = cfg.get("hostname_prefix") or "pi-"
    used = [r.get("hostname", "") for r in read_provision_registry(100000)]
    with PROV_LOCK:
        used += [j["hostname"] for j in PROV_JOBS.values()]
    nums = [int(m.group(1)) for h in used
            for m in [re.fullmatch(re.escape(prefix) + r"(\d+)", h)] if m]
    return f"{prefix}{(max(nums) + 1) if nums else 1:03d}"


def provisioning_active():
    with PROV_LOCK:
        return any(j["state"] == "running" for j in PROV_JOBS.values())


def _follow_provision(dev, proc):
    job = PROV_JOBS[dev]
    for line in proc.stdout:
        line = line.rstrip("\n")
        with PROV_LOCK:
            job["lines"].append(line)
            text = LOG_PREFIX.sub("", line)
            m = re.fullmatch(r"==== (\w+) ====", text)
            if m and m.group(1) in PROVISION_STEPS:
                job["step"] = m.group(1)
            elif text.startswith("Tailscale IP: "):
                job["ip"] = text.split(": ", 1)[1].strip()
            elif text.startswith("Logs: "):
                job["log_dir"] = text.split(": ", 1)[1].strip()
            elif text.startswith("Screen: ws://127.0.0.1:"):
                job["screen_port"] = int(text.rsplit(":", 1)[1])
            elif text.startswith("ERROR"):
                job["error"] = text
            if text.strip() and not text.startswith("$ "):
                job["message"] = text[:300]
    rc = proc.wait()
    with PROV_LOCK:
        if job["stop_requested"]:
            job["state"] = "cancelled"
            job["message"] = "Stopped — the card is incomplete; START it again before use."
        elif rc == 0:
            job["state"] = "done"
            job["message"] = "Done — safe to remove the card."
        else:
            job["state"] = "error"
            job["message"] = job.get("error") or f"provision.py exited {rc} — see the log"
        job["ended"] = time.time()


def card_users(dev, own_pids=()):
    """Other processes still working on `dev`: a provision.py / clone.py run or
    its QEMU VM left running when this server was restarted (e.g. by an
    update) — a second run on the same card would corrupt it."""
    found = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) in own_pids:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                args = f.read().split(b"\0")
        except OSError:
            continue
        cmd = [a.decode(errors="replace") for a in args if a]
        text = " ".join(cmd)
        if ((any(c.endswith(("provision.py", "clone.py")) for c in cmd) and dev in cmd)
                or (any(c.endswith("qemu-system-aarch64") for c in cmd) and f"file={dev}," in text)):
            found.append(f"{pid} ({os.path.basename(cmd[0]) if cmd else '?'})")
    return found


def _own_pids():
    with PROV_LOCK, CLONE_LOCK:
        procs = [j["proc"] for j in list(PROV_JOBS.values()) + list(CLONE_JOBS.values())
                 if j.get("proc") and j["state"] == "running"]
    return {p.pid for p in procs}


def _check_card_free(dev):
    users = card_users(dev, _own_pids())
    if users:
        raise ValueError(f"{dev} is still in use by an earlier run that kept going when PiForge was "
                         f"restarted (process {', '.join(users)}). Let it finish or stop it "
                         f"(sudo kill <pid>), then START again.")


def start_provision(dev, hostname, start_step="flash", layout_name=None):
    if os.geteuid() != 0:
        raise ValueError("server not running as root — restart with sudo")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}", hostname or ""):
        raise ValueError("hostname: letters, digits and '-' only (max 63)")
    if start_step not in PROVISION_STEPS:
        raise ValueError(f"unknown step {start_step!r}")
    if not os.path.exists(PROVISION_PATH):
        raise ValueError("save the settings first")
    card = next((d for d in list_devices() if d["device"] == dev), None)
    if not card:
        raise ValueError(f"{dev} is not a removable card PiForge will touch")
    if FLASH_ACTIVE.is_set():
        raise ValueError("a flash batch is running — wait for it to finish")
    _check_card_free(dev)
    cfg = load_provision_settings()
    config_path = PROVISION_PATH
    layouts = provision_layouts(cfg)
    if layouts:
        if layout_name not in layouts:
            raise ValueError(f"choose a partition layout for {dev}")
        need = layout_min_bytes(layouts[layout_name])
        if card["size"] < need:
            raise ValueError(f"layout {layout_name!r} needs a card of at least {need / _GIB:.1f} GiB — "
                             f"{dev} is {card['size'] / _GIB:.1f} GiB")
        config_path = write_job_config(cfg, dev, layouts[layout_name])
    base_port = int((cfg.get("vm") or {}).get("ssh_port", 2222))
    max_parallel = int(cfg.get("max_parallel") or 2)
    with PROV_LOCK:
        running = {d: j for d, j in PROV_JOBS.items() if j["state"] == "running"}
        if dev in running:
            raise ValueError(f"{dev} is already running")
        if len(running) >= max_parallel:
            raise ValueError(f"already running {len(running)} card(s) (max_parallel = {max_parallel})")
        if any(j["hostname"] == hostname for j in running.values()):
            raise ValueError(f"hostname {hostname} is already in use by a running card")
        used_ports = {j["port"] for j in running.values()}
        port = next(p for p in range(base_port, base_port + 100) if p not in used_ports)
        cmd = [sys.executable, os.path.join(BASE_DIR, "provision.py"), dev,
               "--config", config_path, "--hostname", hostname, "--yes",
               "--from", start_step, "--ssh-port", str(port)]
        # Own process group, so Stop (SIGINT to the group) reaches provision.py
        # and its dd/ssh children — but not QEMU, which provision.py starts in
        # its own session and powers off cleanly itself.
        proc = subprocess.Popen(cmd, cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, bufsize=1, start_new_session=True,
                                env=dict(os.environ, PYTHONUNBUFFERED="1"))
        PROV_JOBS[dev] = {
            "device": dev, "hostname": hostname, "layout": layout_name, "state": "running", "step": start_step,
            "message": "Starting…", "ip": "", "log_dir": "", "error": "", "port": port,
            "started": time.time(), "ended": None, "stop_requested": False, "proc": proc,
            "lines": collections.deque(maxlen=3000),
        }
    threading.Thread(target=_follow_provision, args=(dev, proc), daemon=True).start()


# ---------------------------------------------------------------- clone cards
# clone.py does the work (save a master card as an image, write exact copies);
# this starts one per card, follows its output and stops it.
CLONE_JOBS = {}     # device -> job dict
CLONE_LOCK = threading.Lock()
_CLONE_CAN_RESIZE = []


def _clone_module():
    sys.modules.setdefault("server", sys.modules[__name__])  # see _planner()
    import clone
    return clone


def start_clone(dev, action, name):
    if os.geteuid() != 0:
        raise ValueError("server not running as root — restart with sudo")
    if action not in ("read", "write"):
        raise ValueError(f"unknown clone action {action!r}")
    clone = _clone_module()
    try:
        img, meta = clone.image_paths(name)
    except clone.CloneError as e:
        raise ValueError(str(e))
    if action == "read" and os.path.exists(img):
        raise ValueError(f"an image called {name!r} already exists — pick another name")
    if action == "write" and not os.path.exists(meta):
        raise ValueError(f"no saved image called {name!r}")
    if action == "write":
        with open(meta) as f:
            if json.load(f).get("grow_last") and not clone.e2fs_can_resize():
                raise ValueError("this image was saved with its last partition shrunk, and this PC's e2fsprogs "
                                 "(older than 1.47) can't grow it back. Write it from a PC with Ubuntu 24.04 or newer.")
    if dev not in {d["device"] for d in list_devices()}:
        raise ValueError(f"{dev} is not a removable card PiForge will touch")
    if FLASH_ACTIVE.is_set():
        raise ValueError("a flash batch is running — wait for it to finish")
    with PROV_LOCK:
        if PROV_JOBS.get(dev, {}).get("state") == "running":
            raise ValueError(f"{dev} is busy with a full install")
    _check_card_free(dev)
    with CLONE_LOCK:
        if CLONE_JOBS.get(dev, {}).get("state") == "running":
            raise ValueError(f"{dev} is already busy")
        if action == "write" and any(j["state"] == "running" and j["action"] == "read" and j["name"] == name
                                     for j in CLONE_JOBS.values()):
            raise ValueError(f"{name!r} is still being saved")
        proc = subprocess.Popen([sys.executable, os.path.join(BASE_DIR, "clone.py"), action, dev, "--name", name],
                                cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, bufsize=1, start_new_session=True,
                                env=dict(os.environ, PYTHONUNBUFFERED="1"))
        CLONE_JOBS[dev] = {
            "device": dev, "action": action, "name": name, "state": "running", "step": "",
            "part": "", "percent": None, "message": "Starting…", "error": "",
            "started": time.time(), "ended": None, "stop_requested": False, "proc": proc,
            "lines": collections.deque(maxlen=2000),
        }
    threading.Thread(target=_follow_clone, args=(dev, proc), daemon=True).start()


def _follow_clone(dev, proc):
    job = CLONE_JOBS[dev]
    for line in proc.stdout:
        line = line.rstrip("\n")
        with CLONE_LOCK:
            if line.startswith("PROGRESS "):
                _, part, pct = line.split()
                job["part"], job["percent"] = part, int(pct)
                continue
            if line.startswith("STEP "):
                job["step"], job["part"], job["percent"] = line[5:].strip(), "", None
                continue
            job["lines"].append(line)
            text = LOG_PREFIX.sub("", line)
            if text.startswith("ERROR"):
                job["error"] = text
            if text.strip() and not text.startswith("$ "):
                job["message"] = text[:300]
    rc = proc.wait()
    with CLONE_LOCK:
        if job["stop_requested"]:
            job["state"], job["message"] = "cancelled", "Stopped — the card is incomplete."
        elif rc == 0:
            job["state"] = "done"
        else:
            job["state"] = "error"
            job["message"] = job["error"] or f"clone.py exited {rc} — see the log"
        job["ended"] = time.time()


def stop_clone(dev):
    with CLONE_LOCK:
        job = CLONE_JOBS.get(dev)
        if not job or job["state"] != "running":
            return False
        job["stop_requested"] = True
        try:
            os.killpg(job["proc"].pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return True


def delete_clone_image(name):
    clone = _clone_module()
    img, meta = clone.image_paths(name)
    with CLONE_LOCK:
        if any(j["state"] == "running" and j["name"] == name for j in CLONE_JOBS.values()):
            raise ValueError(f"{name!r} is in use by a running card")
    for path in (img, meta, img + ".part", img + ".lastpart"):
        if os.path.exists(path):
            os.remove(path)


def clone_status():
    clone = _clone_module()
    if not _CLONE_CAN_RESIZE:
        _CLONE_CAN_RESIZE.append(clone.e2fs_can_resize())
    devices = list_devices()
    present = {d["device"] for d in devices}
    now = time.time()
    with CLONE_LOCK:
        for dev in [d for d, j in CLONE_JOBS.items() if j["state"] != "running" and d not in present]:
            del CLONE_JOBS[dev]
        jobs = {d: {k: v for k, v in j.items() if k not in ("proc", "lines")}
                | {"elapsed": format_duration((j["ended"] or now) - j["started"])}
                for d, j in CLONE_JOBS.items()}
    return {"devices": devices, "jobs": jobs, "images": clone.list_images(),
            "can_resize": _CLONE_CAN_RESIZE[0], "flashing": FLASH_ACTIVE.is_set(),
            "root": os.geteuid() == 0}


def clone_log(dev, limit=400):
    with CLONE_LOCK:
        job = CLONE_JOBS.get(dev)
        return list(job["lines"])[-limit:] if job else []


def stop_provision(dev):
    with PROV_LOCK:
        job = PROV_JOBS.get(dev)
        if not job or job["state"] != "running":
            return False
        job["stop_requested"] = True
        job["message"] = "Stopping — powering the VM off cleanly (can take a few minutes)…"
        try:
            os.killpg(job["proc"].pid, signal.SIGINT)
        except ProcessLookupError:
            pass
    return True


def provision_status():
    cfg = load_provision_settings()
    devices = list_devices()
    present = {d["device"] for d in devices}
    now = time.time()
    with PROV_LOCK:
        # A finished card that has been pulled out is done with; forget it so
        # the next card in that reader starts fresh.
        for dev in [d for d, j in PROV_JOBS.items() if j["state"] != "running" and d not in present]:
            del PROV_JOBS[dev]
        jobs = {d: {k: v for k, v in j.items() if k not in ("proc", "lines")}
                | {"elapsed": format_duration((j["ended"] or now) - j["started"])}
                for d, j in PROV_JOBS.items()}
    return {
        "devices": devices,
        "jobs": jobs,
        "checks": provision_checks(cfg),
        "next_hostname": next_hostname(cfg),
        "layouts": layout_choices(cfg),
        "kernel_build": {"state": KERNEL_BUILD["state"], "message": KERNEL_BUILD["message"]},
        "flashing": FLASH_ACTIVE.is_set(),
        "root": os.geteuid() == 0,
    }


# Terminal control codes (colours, cursor moves incl. negative ones, ESC M),
# then any ESC left over where systemd cut a status line off with "…".
_ANSI = re.compile(r"\x1b(\[[-0-9;?]*[ -/]*[@-~]|[()][A-Z0-9]|[=>78MDEc])|\x1b\[?[-0-9;]*")
CONSOLE_TAIL = 256 * 1024   # what a newly opened Screen starts with


def provision_console(dev, name="", offset=0):
    """The VM's screen: its serial console (console-<time>.log in the card's
    log folder, one per boot), read from `offset` of file `name`; the newest
    boot's file when `name` is no longer the newest."""
    with PROV_LOCK:
        job = PROV_JOBS.get(dev)
        log_dir = job.get("log_dir") if job else ""
    files = sorted(glob.glob(os.path.join(log_dir, "console-*.log"))) if log_dir else []
    if not files:
        return {"file": "", "boot": 0, "boots": 0, "offset": 0, "text": "", "reset": True}
    newest = files[-1]
    reset = os.path.basename(newest) != name
    size = os.path.getsize(newest)
    start = max(0, size - CONSOLE_TAIL) if reset or offset > size else offset
    with open(newest, "rb") as f:
        f.seek(start)
        data = f.read(size - start)
    text = _ANSI.sub("", data.decode("utf-8", errors="replace"))
    # systemd redraws its progress lines in place (CR, cursor moves): as plain
    # text that's a stack of blank lines, so collapse them.
    text = re.sub(r"\n[ \t]*(?:\n[ \t]*)+", "\n", text.replace("\r", "\n"))
    return {"file": os.path.basename(newest), "boot": len(files), "boots": len(files),
            "offset": size, "text": text, "reset": reset or offset > size}


def provision_log(dev, limit=400):
    with PROV_LOCK:
        job = PROV_JOBS.get(dev)
        return list(job["lines"])[-limit:] if job else []


def start_kernel_build():
    if KERNEL_BUILD["state"] == "running":
        raise ValueError("a kernel build is already running")
    script = os.path.join(BASE_DIR, "vm-kernel", "build-vm-kernel.sh")
    KERNEL_BUILD.update(state="running", message="Starting…")
    KERNEL_BUILD["lines"].clear()

    def runner():
        os.makedirs(KERNEL_OUT_DIR, exist_ok=True)
        proc = subprocess.Popen(["bash", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, env=dict(os.environ, OUT=KERNEL_OUT_DIR))
        for line in proc.stdout:
            KERNEL_BUILD["lines"].append(line.rstrip("\n"))
            if line.startswith(">>>") or line.startswith("ERROR"):
                KERNEL_BUILD["message"] = line.strip()[:200]
        rc = proc.wait()
        # Built as root; hand the cache back to the user it belongs to.
        for root, dirs, files in os.walk(os.path.dirname(KERNEL_OUT_DIR)):
            for name in dirs + files:
                _chown_real(os.path.join(root, name))
        _chown_real(os.path.dirname(KERNEL_OUT_DIR))
        if rc == 0:
            KERNEL_BUILD.update(state="done", message="VM kernel built ✓")
        else:
            KERNEL_BUILD.update(state="error", message=KERNEL_BUILD["message"] or f"build failed (exit {rc})")

    threading.Thread(target=runner, daemon=True).start()


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(BASE_DIR, "index.html"), "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except FileNotFoundError:
                self._json({"error": "index.html missing"}, 404)
        elif path.startswith("/vendor/novnc/"):
            # Bundled noVNC (the VM screen viewer): plain files, no directory escape.
            root = os.path.realpath(os.path.join(BASE_DIR, "vendor", "novnc"))
            full = os.path.realpath(os.path.join(root, path[len("/vendor/novnc/"):]))
            types = {".js": "text/javascript", ".txt": "text/plain"}
            if not full.startswith(root + os.sep) or not os.path.isfile(full) or os.path.splitext(full)[1] not in types:
                return self._json({"error": "not found"}, 404)
            with open(full, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", types[os.path.splitext(full)[1]] + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/devices":
            try:
                self._json({
                    "devices": list_devices(),
                    "flashing": FLASH_ACTIVE.is_set(),
                    "prep": PREP_STATE,
                    "image_cached": os.path.exists(RAW_IMAGE),
                    "image_url": current_image_url(),
                    "root": os.geteuid() == 0,
                    "provisioning": provisioning_active(),
                })
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif path == "/api/config":
            # Prefill values for the UI form. config.json (gitignored) wins
            # over config.example.json wins over generic built-in defaults —
            # nothing here is ever the repo's real Wi-Fi password.
            self._json(load_config_defaults())
        elif path == "/api/profiles":
            self._json(load_profiles())
        elif path == "/api/history":
            limit = int(query.get("limit", ["50"])[0])
            self._json({"rows": read_history(limit)})
        elif path == "/api/provision/status":
            try:
                self._json(provision_status())
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif path == "/api/provision/settings":
            self._json({"config": load_provision_settings(),
                        "saved": os.path.exists(PROVISION_PATH), "path": PROVISION_PATH})
        elif path == "/api/provision/log":
            self._json({"lines": provision_log(query.get("device", [""])[0])})
        elif path == "/api/provision/console":
            self._json(provision_console(query.get("device", [""])[0], query.get("file", [""])[0],
                                         int(query.get("offset", ["0"])[0] or 0)))
        elif path == "/api/provision/kernel-log":
            self._json({"lines": list(KERNEL_BUILD["lines"])})
        elif path == "/api/clone/status":
            try:
                self._json(clone_status())
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif path == "/api/clone/log":
            self._json({"lines": clone_log(query.get("device", [""])[0])})
        elif path == "/api/provision/images":
            self._json(list_image_tars(load_provision_settings()))
        elif path == "/api/provision/registry":
            self._json({"rows": read_provision_registry()})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        if parsed.path == "/api/provision/images/upload":
            # Raw file body, streamed straight to disk (image tars can be GBs).
            try:
                name = urllib.parse.parse_qs(parsed.query).get("name", [""])[0]
                save_image_tar(name, self.rfile, length)
                self._json({"ok": True})
            except Exception as e:
                self.close_connection = True  # the rest of the body may still be unread
                self._json({"error": str(e)}, 400)
            return
        body = self.rfile.read(length) if length else b"{}"

        if parsed.path == "/api/profiles":
            try:
                req = json.loads(body)
                name = (req.get("name") or "").strip()
                if not name:
                    raise ValueError("profile name is required")
                profiles = load_profiles()
                profiles[name] = req["config"]
                save_profiles(profiles)
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return

        if parsed.path == "/api/cancel":
            try:
                req = json.loads(body)
                cancelled = request_cancel(req.get("devices") or None)
                self._json({"ok": True, "devices": cancelled})
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return

        if parsed.path.startswith("/api/clone/"):
            try:
                req = json.loads(body)
                action = parsed.path[len("/api/clone/"):]
                if action == "start":
                    start_clone(req["device"], req.get("action"), (req.get("name") or "").strip())
                elif action == "stop":
                    if not stop_clone(req["device"]):
                        raise ValueError("that card is not running")
                elif action == "delete":
                    delete_clone_image(req.get("name"))
                else:
                    return self._json({"error": "not found"}, 404)
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return

        if parsed.path.startswith("/api/provision/"):
            try:
                req = json.loads(body)
                action = parsed.path[len("/api/provision/"):]
                if action == "settings":
                    cfg = req["config"]
                    if not isinstance(cfg, dict):
                        raise ValueError("settings must be a JSON object")
                    validate_layouts(cfg)
                    save_provision_settings(cfg)
                elif action == "start":
                    start_provision(req["device"], (req.get("hostname") or "").strip(),
                                    req.get("from") or "flash", req.get("layout"))
                elif action == "stop":
                    if not stop_provision(req["device"]):
                        raise ValueError("that card is not running")
                elif action == "images/delete":
                    delete_image_tar(req.get("name"))
                elif action == "build-kernel":
                    if os.geteuid() != 0:
                        raise ValueError("server not running as root — restart with sudo")
                    start_kernel_build()
                else:
                    return self._json({"error": "not found"}, 404)
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return

        if parsed.path != "/api/flash":
            return self._json({"error": "not found"}, 404)
        if os.geteuid() != 0:
            return self._json({"error": "server not running as root — restart with sudo"}, 403)
        if FLASH_ACTIVE.is_set():
            return self._json({"error": "flash already in progress"}, 409)
        try:
            req = json.loads(body)
            devices = req["devices"]
            cfg = req["config"]
            for key in ("hostname", "user", "password", "timezone"):
                if not cfg.get(key):
                    raise ValueError(f"missing config field: {key}")
            # Wi-Fi is optional (Ethernet-only boards); country is required
            # only if a network is actually being configured.
            if cfg.get("wifi_ssid") and not cfg.get("wifi_country"):
                raise ValueError("wifi_country is required when wifi_ssid is set")
            if cfg.get("static_ip_base"):
                compute_static_ip(cfg["static_ip_base"], 1)  # validates format early
            if not devices:
                raise ValueError("no devices selected")
            with PROV_LOCK:
                busy = [d for d in devices if PROV_JOBS.get(d, {}).get("state") == "running"]
            if busy:
                raise ValueError(f"{', '.join(busy)} is being fully installed — stop it first")
            start_flash(devices, cfg)
            self._json({"ok": True, "count": len(devices)})
        except Exception as e:
            self._json({"error": str(e)}, 400)

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/profiles":
            return self._json({"error": "not found"}, 404)
        name = urllib.parse.parse_qs(parsed.query).get("name", [""])[0]
        profiles = load_profiles()
        if name in profiles:
            del profiles[name]
            save_profiles(profiles)
        self._json({"ok": True})


if __name__ == "__main__":
    addr = ("127.0.0.1", PORT)
    print(f"Serving on http://{addr[0]}:{addr[1]}  (root: {os.geteuid() == 0})")
    if os.geteuid() != 0:
        print("WARNING: not root — device detection works, flashing will be refused.")
    ThreadingHTTPServer(addr, Handler).serve_forever()
