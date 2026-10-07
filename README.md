# PiForge

[![CI](https://github.com/gokul-hastrophil/piforge/actions/workflows/ci.yml/badge.svg)](https://github.com/gokul-hastrophil/piforge/actions/workflows/ci.yml)
[![CodeQL](https://github.com/gokul-hastrophil/piforge/actions/workflows/codeql.yml/badge.svg)](https://github.com/gokul-hastrophil/piforge/actions/workflows/codeql.yml)
[![Latest release](https://img.shields.io/github/v/release/gokul-hastrophil/piforge)](https://github.com/gokul-hastrophil/piforge/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Flash Raspberry Pi OS to many SD cards at once — in parallel, with hostname,
user, password, Wi-Fi, country, timezone, keyboard layout, SSH keys and even
per-card static IPs pre-configured, so every card boots straight to a working
login. No setup wizard, no per-card manual typing.

Built for anyone provisioning more than one Raspberry Pi at a time: classroom
kits, workshops, IoT fleets, cluster builds.

## Contents

- [Install as a system app](#install-as-a-system-app)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Setup](#setup)
- [Web UI (recommended, fastest)](#web-ui-recommended-fastest)
- [CLI only (no browser, no server)](#cli-only-no-browser-no-server)
- [Injecting config into an already-flashed card](#injecting-config-into-an-already-flashed-card)
- [Full provisioning in a VM](#full-provisioning-in-a-vm)
- [Cloning a finished card](#cloning-a-finished-card)
- [Config field reference](#config-field-reference)
- [Safety](#safety)
- [Project layout](#project-layout)
- [Contributing](#contributing)
- [Security](#security)
- [License](#license)

## Install as a system app

### Debian, Ubuntu, Raspberry Pi OS (.deb)

Install PiForge like any other desktop app — the same way Raspberry Pi
Imager ships its own `.deb`:

```bash
./packaging/install.sh
```

That builds the package (if not already built) and installs it through
`apt`, which resolves and installs every dependency automatically —
`python3-gi`, `webkit2gtk`, `jq`, `wpasupplicant`, all of it, no separate
steps. **Important:** always install through `apt`, not `dpkg -i` directly.
`dpkg -i` does *not* auto-install missing dependencies — it just records
them as unmet and leaves the package half-configured, which is the classic
reason a manually-installed `.deb` "doesn't install its dependencies".
If you'd rather run the two steps yourself instead of the convenience
script, that's the same thing under the hood:

```bash
./packaging/build-deb.sh
sudo apt install ./packaging/dist/piforge_1.0.0_all.deb   # apt, not dpkg -i
```

Either way, you get a **PiForge** entry in your application menu. Click it
and it's ready to use — no terminal, no manual `sudo python3 server.py`:

- The app itself (`/opt/piforge`) is read-only and shared by every user on
  the machine.
- Each user's own settings live in their own `~/.config/piforge/` and
  `~/rpi-images/` — never mixed with another user's, and never inside
  `/opt/piforge`.
- Flashing needs root (raw block-device writes), so the launcher opens a
  native graphical password prompt (`pkexec`) the first time — no terminal
  needed. The server then keeps running quietly in the background
  (bound to `127.0.0.1` only) so relaunching PiForge later reuses it
  instantly instead of prompting again.
- It opens as a genuine native window (GTK3 + WebKit2) — its own title
  bar, its own entry in the taskbar/dock with the app icon, no address
  bar, no tabs, no browser right-click menu. Not a browser window
  pretending to be an app; there's no browser involved at all. Closing it
  warns first if a card is still writing, so you can't accidentally kill
  a flash in progress.

Uninstall with `sudo apt remove piforge`. To rebuild after making changes,
just rerun `./packaging/build-deb.sh` — it regenerates the package fresh
from whatever's currently in the repo (bump the version first: edit
`VERSION`).

### Fedora, RHEL-family (.rpm)

```bash
./packaging/build-rpm.sh
sudo dnf install packaging/rpm-dist/piforge-*.noarch.rpm
```

Same app, same behavior as the `.deb` above — just packaged for `dnf`.
Package names are verified against Fedora's live repositories; if you're
adapting this for openSUSE, the WebKitGTK/PyGObject package names differ
(see the comments in `packaging/rpm/piforge.spec`) — the app code itself
needs no changes, only the spec's `Requires:`.

Uninstall with `sudo dnf remove piforge`.

### Arch Linux (PKGBUILD)

```bash
cd packaging/arch
makepkg -si
```

This follows normal AUR convention — `makepkg` downloads a specific
tagged release tarball and verifies it against a pinned checksum, unlike
the `.deb`/`.rpm` scripts above which build from your current checkout.
That means `packaging/arch/PKGBUILD`'s `pkgver`/`sha256sums` only track
tagged releases, not every commit — routine PKGBUILD maintenance, not a
bug. Uninstall with `sudo pacman -R piforge`.

Prefer the manual workflow, or you're on a distro none of the above
cover? See **Web UI** below — the packaged app and running `server.py`
directly are
the exact same code, just launched differently.

## How it works

1. You start a small local web server (`server.py`, Python stdlib only — no
   pip installs).
2. Open `http://127.0.0.1:47823` in a browser. It auto-detects every
   removable card in your USB reader(s) and lists them, flagging any that
   already contain data.
3. Fill in the card-setup form (or load a saved profile), select the cards,
   hit **START**.
4. The OS image is downloaded once and decompressed once. Every selected
   card is then written **in parallel**, straight from RAM (page cache) —
   so writing 4 cards takes about the same time as writing 1.
5. Each card gets a unique hostname (`raspberrypi1`, `raspberrypi2`, ...) plus
   your configured user/password/Wi-Fi/timezone/keyboard/SSH, injected the
   same way the official Raspberry Pi Imager does it (`firstrun.sh` +
   `cmdline.txt`).
6. Boot the card — it applies the config, reboots once, and you're at a
   normal login. No wizard.

## Requirements

Linux only (uses `lsblk`, `/sys/block/*/stat`, `dd oflag=direct`,
`blockdev`, `partprobe` — none of which exist on macOS/Windows). Tested on
Ubuntu/Debian-family distros.

Run this once after cloning:

```bash
./check-requirements.sh
```

It checks for: `python3`, `lsblk`, `dd`, `xz`, `openssl`, `wpa_passphrase`,
`partprobe`, `jq`, `curl` (all from standard repos — e.g.
`sudo apt install python3 util-linux coreutils xz-utils openssl wpasupplicant parted jq curl`).
`rpi-imager` is optional, only used by the alternate CLI path (`flash-all.sh`).

## Setup

```bash
cp config.example.json config.json
```

Edit `config.json` with your real values — see the field reference below.
It's gitignored; your real password, Wi-Fi credentials, and SSH key never
get committed.

Running from a repo checkout, `config.json`/`profiles.json` next to
`server.py` always take priority if present (this workflow). Running the
installed `.deb` instead, where `/opt/piforge` ships only the `.example`
templates, PiForge falls back to `~/.config/piforge/` — resolved against
the actual invoking user (via `$SUDO_USER`/`$PKEXEC_UID`), not `root`, even
though the server itself must run as root to write block devices. Same
for the downloaded OS image cache and flash history: always under the real
user's own `~/rpi-images/`, never `/root/rpi-images/`.

## Web UI (recommended, fastest)

```bash
sudo python3 server.py
```

Open `http://127.0.0.1:47823`. Insert SD cards — they're detected
automatically and pre-selected. Adjust the form if needed, then **START**.

Root is required only for actually writing to block devices; device
detection and the page itself work without it (flashing is refused with a
clear error until you restart with `sudo`).

### Why parallel writes are fast

The image is decompressed exactly once (`xz -T0`, using every CPU core) into
a raw `.img`, which typically fits in RAM (~6 GB for the standard desktop
image) after the first flash. Every card then reads that image straight from
the kernel's page cache via `dd`, so N cards write concurrently at close to
each card's own hardware speed — not sequentially, and not re-paying the
decompression cost per card.

### Feature reference

- **OS image picker** — choose from Raspberry Pi Foundation's official
  "latest" links (Desktop/Lite/Full × current/Legacy Bookworm) or paste any
  custom `.img.xz` URL. Switching the image automatically invalidates the
  cached download so the new one is fetched.
- **Card capacity check** — before writing, each card's real size is
  compared against the image size. A card too small is refused with a clear
  error instead of silently getting a truncated, unbootable write.
- **Wi-Fi country / timezone / keyboard layout** — proper dropdowns (not
  free text), so you can't typo a country code. Timezone list comes from
  your browser's live IANA database when available.
- **Password eye icons** — click to reveal the password/Wi-Fi-password
  fields before submitting.
- **SSH access** (collapsible section) — paste one or more public keys to
  install into `~/.ssh/authorized_keys`; optionally disable password login
  entirely (key-only). Providing a key auto-enables SSH even if the
  checkbox is off.
- **Static IP** (collapsible section) — set a base address
  (e.g. `192.168.50.10`) and every card gets that address +1 per card,
  matching the same numbering as hostnames. Writes a `dhcpcd.conf` static
  profile for the interface you choose (`eth0`/`wlan0`).
- **Profiles** — save the whole form under a name (e.g. `classroom-kit`,
  `robot-cluster`) via **Save as**, reload it any time from the dropdown,
  delete with 🗑. Stored server-side in `profiles.json` (gitignored) so
  profiles persist across browsers/machines using the same server, unlike
  the localStorage-based "Remember settings".
- **Remember settings** — separately, saves your current form to this
  browser's `localStorage` so a page refresh doesn't lose your edits.
  **Reset** clears it and reloads `config.json`'s defaults.
- **Verify after write** — full byte-for-byte readback comparison after
  writing (roughly doubles time per batch). Off by default for speed;
  recommended for production/unattended batches.
- **Live speed + ETA** — each card's progress message shows current MB/s
  and estimated time remaining, not just a percentage.
- **Has-data warning** — a card that already contains recognizable
  filesystems is flagged with a badge and named explicitly in the erase
  confirmation dialog.
- **Retry** — a card that failed gets a one-click **↻ Retry** button that
  re-flashes just that card with the current form settings, keeping its
  original hostname/IP index rather than renumbering it as card 1.
- **History** — collapsible panel showing the last 50 cards flashed
  (timestamp, device, hostname, status, duration), read from
  `~/rpi-images/flash-history.csv`.
- **Browser notification** — check "Notify me when the batch finishes" to
  get a system notification with a done/failed summary when a batch
  completes (useful if you tab away during a big run).

## CLI only (no browser, no server)

```bash
sudo ./flash-all.sh /dev/sda /dev/sdb /dev/sdc
```

Requires `rpi-imager`. Simpler and more portable, but slower than the web UI
(rpi-imager decompresses and verifies per card rather than sharing one
decompressed image across all cards), and doesn't have the profiles/history/
retry/notification features — those are web-UI only.

## Injecting config into an already-flashed card

If a card was flashed by something else (another tool, a different
pipeline step that repartitions the boot volume afterward, etc.) and boots
into the first-run setup wizard instead of your configured user, that means
`firstrun.sh` and the `cmdline.txt` patch didn't survive to boot time —
usually because a later step overwrote the boot partition. Re-inject as the
very last step before the card is removed:

```bash
sudo ./inject-config.sh /dev/sda            # finds and mounts the boot partition
# or, if already mounted:
./inject-config.sh /media/you/bootfs
```

Optionally pass a hostname as a second argument to override the one in
`config.json`.

## Full provisioning in a VM

Flashing gives you a card that boots to a login. `provision.py` goes further:
it boots the card **on this PC** in a QEMU arm64 VM and runs your own install
scripts inside it — packages, databases, Docker images, Tailscale — so the
card is fully installed before it ever reaches a Pi. One card at a time, from
the command line.

What it does, in order (`--from <step>` resumes at any of them):

| Step | What happens |
|---|---|
| `flash` | Writes the OS image (same cache as the web UI) |
| `partition` | Grows root and adds the partitions in `layout`, keeping the disk ID so PARTUUIDs stay valid |
| `inject` | Same `firstrun.sh` as the web UI (user, hostname, SSH keys), and turns off Pi OS's first-boot auto-resize |
| `boot` | First boot in the VM applies `firstrun.sh` and reboots once; then waits for SSH on `127.0.0.1:<ssh_port>` |
| `install` | Mounts host folders (`shares`, read-only 9p), copies `package` in, runs `install_cmd` as root |
| `tailscale` | Installs Tailscale and joins with `auth_key` — the card keeps that identity and IP on the real Pi. Skipped when `auth_key` is empty (e.g. the card joins Tailscale on the Pi itself) |
| `verify` | Runs every `verify_cmds` entry; any failure fails the card |
| `finish` | Removes the VM-only files, powers the VM off cleanly, flushes the card — always runs, even after an error |

Every card is logged under `~/rpi-images/provision/<hostname>-<time>/` (the
run log plus the VM's serial console) and recorded in
`~/rpi-images/provisioned-cards.csv` with its Tailscale IP.

**What can't run in the VM:** anything that needs the Pi's hardware — kernel
drivers for Pi add-ons, devices like `/dev/hailo0`, EEPROM, the hardware
watchdog, or anything keyed to the Pi's own MAC address. The VM runs its own
kernel, so `uname -r` is not the Pi's. Have `install_cmd` leave those steps
for a first-boot service on the Pi.

### In the app

The **Full install** tab (next to **Flash cards**) does all of this without a
terminal: a station check (QEMU, VM kernel — with a one-click **Build VM
kernel**, Docker image folder, install scripts, password), the
settings, and every inserted card with a hostname box, a partition **Layout** and **START**. Each card picks the largest layout that fits it (a layout that's too big can't be started); **➕** adds a new layout, saved with the settings for every card. Each card
shows its live step, a log, **Stop** (powers the VM off cleanly), **Retry from**
any step, and its Tailscale IP when done; 🧾 lists every card provisioned.

To hand a ready-made station build to someone, put your settings in
`provision.defaults.json` in the repo root (gitignored) and build the VM
kernel before `./packaging/build-deb.sh` — the `.deb` then ships both, so a new
station starts pre-configured. That file may hold the card password and
Tailscale key: only give that `.deb` to people who may have them.

### One-time setup

```bash
sudo apt install qemu-system-arm e2fsprogs util-linux xz-utils openssh-client \
                 gcc-aarch64-linux-gnu make flex bison bc libssl-dev libelf-dev curl
./vm-kernel/build-vm-kernel.sh     # ~15-40 min, once -> ~/.cache/piforge/vm-kernel/Image
cp provision.example.json provision.json   # edit it; gitignored
```

The VM kernel is a plain upstream arm64 kernel with everything built in
(no modules), since the card's `/lib/modules` only has Pi kernels.

### Per card

```bash
sudo python3 provision.py --config provision.json --hostname mill-01 /dev/sdX
```

Only removable cards the web UI would list are accepted, and you're asked to
type `yes` before anything is erased (`--yes` skips that). It's slow — the
VM emulates ARM on x86 — so expect a full install to take hours rather than
minutes. To do two cards at once, run two copies with different
`vm.ssh_port` values in two config files.

### Config fields

| Field | Meaning |
|---|---|
| `image` | OS image URL (cached like the web UI) or a local `.img` / `.img.xz` |
| `card` | Same fields as `config.json`: `user`, `password`, `timezone`, `keymap`, `ssh_authorized_key`, Wi-Fi |
| `layout.root_size` | New size of the root partition (it can only grow) |
| `layout.partitions` | Extra partitions: `num` 3 (primary), then 5, 6, ... (logical, in an extended partition 4); `size` like `30G`, or `"rest"` for the last one. They're created empty — format them from your install scripts |
| `layouts` | Named layouts the app offers per card, e.g. `{"256 GB": {...}, "128 GB": {...}}`, each shaped like `layout`. The app writes the chosen one into that card's own settings as `layout`; the command line uses `layout` |
| `vm.kernel` / `cpus` / `memory` / `ssh_port` | QEMU settings |
| `vm.disk_alias` | In the VM the card is `/dev/vda`; this adds `/dev/<alias>` and `/dev/<alias>pN` symlinks for scripts that hardcode the Pi's name (default `mmcblk0`). VM-only |
| `vm.keep_provision_key` | `true` keeps PiForge's own SSH key on the card (removed by default) |
| `shares` | Host folders mounted read-only in the VM: `tag`, `host_path`, `guest_path` |
| `package` | Copied into the VM before the install: `local_dir` + `include` (list of paths), or `zip_url`; lands in `guest_dir` |
| `install_cmd` / `install_env` | Run as root in the VM, with these environment variables |
| `tailscale` | `auth_key` (use a reusable, pre-approved, **tagged** key so devices don't expire) and `extra_args` for `tailscale up` |
| `verify_cmds` | Shell commands run as root in the VM after everything; each must exit 0 |

Always let `provision.py` finish (or run `--from finish`) before removing a
card — pulling a card out from under a running VM corrupts it.

## Cloning a finished card

Instead of installing every card, make **one master card** (on a real Pi, the
normal way), save it as an image, and write exact copies. The **Clone cards**
tab does both:

- **💾 Save image** reads the master (it isn't changed) into
  `~/rpi-images/clones/<name>.img`: the partition table, FAT partitions in
  full, and only the *used* blocks of each ext4 partition (`e2image -ra`), so
  the image is about the size of the data.
- **▶ Write copy** / **Write to all cards** writes it to blank cards, several
  at once, with the same disk ID (PARTUUIDs in `cmdline.txt` and `fstab` stay
  valid), then checks every ext4 partition.

With e2fsprogs 1.47+ (Ubuntu 24.04) the image's last partition is shrunk to its
data and grown to fill each copy, so copies fit any card big enough for the
data. Older e2fsprogs (Ubuntu 22.04) can't check or resize current Pi OS ext4:
images keep the master's size and copies need cards at least that big.

Copies are identical to the master — hostname, machine-id, SSH host keys and
anything tied to the master Pi (a license bound to its MAC, Tailscale
identity) included. Give each Pi its own after the copy, on the Pi itself
(for Supernova: `sn-pi-setup.sh` in the supernova-production repo).

Command line: `sudo python3 clone.py read /dev/sdX --name master-v1`,
`sudo python3 clone.py write /dev/sdY --name master-v1`, `python3 clone.py list`.

## Config field reference

See `config.example.json` for the full set with safe defaults. Notable ones
beyond the obvious hostname/user/password/Wi-Fi:

| Field | Meaning |
|---|---|
| `image_url` | Any Raspberry Pi OS `.img.xz` link. Get current links from https://www.raspberrypi.com/software/operating-systems/, or use one of the UI's built-in presets. |
| `ssh_authorized_key` | One or more public keys (newline-separated) to install for the configured user. |
| `disable_ssh_password` | `true` to require key-only SSH login. |
| `static_ip_base` / `static_ip_cidr` / `static_ip_gateway` / `static_ip_dns` / `static_ip_iface` | Static networking; leave `static_ip_base` empty to use DHCP (default). |
| `number_hostnames` | `false` to give every card in a batch the identical hostname — fine for one card, a network name conflict for more than one. |
| `verify` | `true` to always byte-verify after writing. |

Profiles use the same fields; see `profiles.example.json` for two sample
profiles you can copy to `profiles.json` and adapt (or just build them from
the UI's **Save as**).

## Safety

- Only removable block devices are ever listed or written — the device list
  actively excludes anything mounted at `/`, `/boot`, `/boot/firmware`,
  `/home`, or under `/usr`. Fixed internal disks never appear as flashable
  targets.
- A card smaller than the image is refused before any write starts.
- The web UI requires an explicit confirmation dialog naming every device
  (and any data already on it) before erasing anything.
- `flash-all.sh` requires typing `yes` before it touches any device.
- Still — this tool **permanently erases everything** on the cards you
  select. Double-check `lsblk` output before confirming if you have any
  doubt about which device is which.

## Project layout

| File | Purpose |
|---|---|
| `server.py` | Web UI backend: device detection, parallel `dd` flashing, profiles, history, config injection |
| `index.html` | Web UI frontend |
| `firstrun_gen.py` | Single shared implementation of the `firstrun.sh` template (hostname, user, SSH keys, Wi-Fi, static IP, timezone, keyboard) — used by `server.py`, `inject-config.sh`, and `flash-all.sh` so there's one place to trust, not three |
| `flash-all.sh` | CLI-only alternative using `rpi-imager --cli` |
| `inject-config.sh` | Re-inject config into an already-flashed card |
| `provision.py` | Full provisioning in a QEMU arm64 VM: flash, partition, install, Tailscale, verify |
| `provision.example.json` | Template for `provision.py` — copy to `provision.json` and edit |
| `clone.py` | Save a finished card as an image and write exact copies (the Clone cards tab) |
| `vm-kernel/build-vm-kernel.sh` | Builds the arm64 kernel `provision.py` boots cards with |
| `config.example.json` | Template — copy to `config.json` and edit |
| `profiles.example.json` | Sample named presets — copy to `profiles.json`, or just build them from the UI |
| `check-requirements.sh` | Verifies all required tools are installed |
| `VERSION` | Single source of truth for the package version |
| `packaging/build-deb.sh` | Builds the `.deb` from the current repo contents |
| `packaging/install.sh` | Builds (if needed) and installs via `apt`, so dependencies resolve automatically |
| `packaging/piforge` | Native GTK3+WebKit2 app installed as `/usr/bin/piforge` — starts the server via `pkexec` and shows the UI in a real app window |
| `packaging/debian/piforge.desktop` | Application-menu entry |
| `packaging/piforge.svg` | App icon |
| `packaging/piforge-server-root` | One-line root-helper wrapper invoked via `pkexec`, matched to the polkit action below so the auth prompt is branded instead of generic |
| `packaging/debian/io.github.gokul-hastrophil.piforge.policy` | polkit action definition — gives the `pkexec` password prompt a proper "PiForge needs root to flash SD cards" message and icon |
| `packaging/rpm/piforge.spec` | RPM spec (Fedora/RHEL-family) |
| `packaging/build-rpm.sh` | Builds the `.rpm` from the current repo contents |
| `packaging/arch/PKGBUILD` | Arch Linux package definition (`makepkg`) |

## Contributing

Bug reports, feature requests, and PRs are welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, the testing checklist
this project actually follows, and commit/PR conventions. Every PR runs
automated CI (syntax checks + CodeQL) and an AI-assisted review before
merge; see [CONTRIBUTING.md](CONTRIBUTING.md#review-process) for what that
covers and what it doesn't replace.

All contributors and participants are expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Security

This tool runs as root and writes raw block devices — see
[SECURITY.md](SECURITY.md) for the threat model and how to report a
vulnerability privately rather than in a public issue.

## License

MIT — see `LICENSE`.
