# PiForge

[![CI](https://github.com/gokul-hastrophil/piforge/actions/workflows/ci.yml/badge.svg)](https://github.com/gokul-hastrophil/piforge/actions/workflows/ci.yml)
[![CodeQL](https://github.com/gokul-hastrophil/piforge/actions/workflows/codeql.yml/badge.svg)](https://github.com/gokul-hastrophil/piforge/actions/workflows/codeql.yml)
[![Latest release](https://img.shields.io/github/v/release/gokul-hastrophil/piforge)](https://github.com/gokul-hastrophil/piforge/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Flash Raspberry Pi OS to many SD cards at once — in parallel, with hostname,
user, password, Wi-Fi, country, timezone, keyboard layout, SSH keys and even
per-card static IPs pre-configured, so every card boots straight to a working
login. No setup wizard, no per-card manual typing.

Three stages, one app: **1 · Flash OS** (the above), **2 · Partition cards**
(cut a custom GPT/MBR layout on a card — blank or already-flashed — including
resizing an existing partition), and **3 · Tailscale** (mint and install a
per-card Tailscale key on an already-prepared card, so it joins your tailnet
on first boot with no manual `tailscale up`).

Built for anyone provisioning more than one Raspberry Pi at a time: classroom
kits, workshops, IoT fleets, cluster builds.

## Contents

- [Install as a system app](#install-as-a-system-app)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Setup](#setup)
- [Web UI (recommended, fastest)](#web-ui-recommended-fastest)
- [Partition cards (stage 2)](#partition-cards-stage-2)
- [Tailscale provisioning (stage 3)](#tailscale-provisioning-stage-3)
- [Profiles (one profile, both stages)](#profiles-one-profile-both-stages)
- [CLI only (no browser, no server)](#cli-only-no-browser-no-server)
- [Injecting config into an already-flashed card](#injecting-config-into-an-already-flashed-card)
- [Config field reference](#config-field-reference)
- [Safety](#safety)
- [Project layout](#project-layout)
- [Troubleshooting](#troubleshooting)
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
sudo apt install ./packaging/dist/piforge_1.2.0_all.deb   # apt, not dpkg -i
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
`sudo apt install python3 util-linux coreutils xz-utils openssl wpasupplicant parted jq curl`),
plus, for stages 2 and 3: `mkfs.vfat`/`mkfs.ext4`/`resize2fs`/`e2fsck`
(`dosfstools`/`e2fsprogs` — partition formatting and root-partition resize)
and `gpg` (`gnupg` — encrypts the per-card Tailscale key onto the card).
`rpi-imager` is optional, only used by the alternate CLI path (`flash-all.sh`).

## Setup

```bash
cp config.example.json config.json
```

Edit `config.json` with your real values — see the field reference below.
It's gitignored; your real password, Wi-Fi credentials, and SSH key never
get committed. `profiles.json`, `partition_profiles.json`, and
`tailscale_config.json` follow the exact same pattern (each has its own
`.example.json` template) and are all optional — only needed if you want
saved profiles, saved partition layouts, or stage 3's station settings,
respectively.

Running from a repo checkout, these files next to `server.py` always take
priority if present (this workflow). Running the installed `.deb` instead,
where `/opt/piforge` ships only the `.example` templates, PiForge falls back
to `~/.config/piforge/` — resolved against the actual invoking user (via
`$SUDO_USER`/`$PKEXEC_UID`), not `root`, even though the server itself must
run as root to write block devices. Same for the downloaded OS image cache
and flash history: always under the real user's own `~/rpi-images/`, never
`/root/rpi-images/`.

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
- **Profiles** — save the whole form *and* the current partition layout
  together under one name (e.g. `classroom-kit`, `robot-cluster`) via
  **Save as**; the same profile applies on both the Flash OS and Partition
  cards tabs — pick it once. Reload any time from the dropdown, delete with
  🗑. Stored server-side (`profiles.json` + `partition_profiles.json`,
  gitignored) so profiles persist across browsers/machines using the same
  server, unlike the localStorage-based "Remember settings". The header ⬆/⬇
  icons export/import everything (flash config + partition layout +
  Tailscale settings) as one JSON file — see
  [Profiles](#profiles-one-profile-both-stages) below.
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

## Partition cards (stage 2)

For anything beyond "flash the whole card as one OS image" — a card that
needs its own data partitions alongside the OS, or a fleet where every card
needs the same custom layout. Works on a blank card (wipes it) or an
already-flashed one (keeps its existing partitions and adds new ones after
them) — same tab, controlled by **Existing partitions to keep**.

- **GPT or MBR**, chosen per layout. MBR automatically inserts the one
  extended container a run of logical partitions needs, and numbers
  everything exactly the way `parted` itself would (primaries first, then
  the extended container, then logicals starting at 5) — you never have to
  think about partition numbers yourself. MBR's 4-primary-slot limit
  (extended container included) is checked up front, before anything is
  written.
- **Per-partition sizing** — percent of the card, an exact size in MiB, or
  "remainder" (fill whatever's left); mix and match freely (e.g. "boot: 10%,
  data: remainder").
- **Existing partitions to keep** — `0` wipes the whole card and starts
  fresh. Any higher number keeps that many existing partitions untouched and
  places new ones directly after them — the same shape as taking an
  already-flashed OS card and adding data partitions to it. Any old
  partitions past the kept count are removed first, so re-running a layout
  on a previously-provisioned card doesn't collide with leftovers from a
  prior run.
- **Resize the last kept partition** — optionally grow or shrink the last
  *kept* partition (typically root) to an exact size in MiB before the new
  partitions are placed after it. Shrinking checks the filesystem
  (`e2fsck`), shrinks it well below the new boundary, re-cuts the partition
  at the exact target, then grows the filesystem back to fill it exactly —
  `parted` can't shrink a partition table entry in place, so the filesystem
  is always resized to the safe side of the boundary before the table
  changes. Growing just widens the partition then grows the filesystem to
  fill it. ext4 only.
- **Formatting** — each new partition is formatted per its chosen
  filesystem (`fat32`/`fat16`/`ext4`/`ext3`/`exfat`/`ntfs`/`linux-swap`), or
  left raw/unformatted.
- **Read current layout** — before deciding on "existing partitions to
  keep", inspect a selected card's real current partition table (numbers,
  sizes, filesystems, flags) right in the UI.
- **Saved layouts** — same profile mechanism as stage 1; see
  [Profiles](#profiles-one-profile-both-stages) below.

## Tailscale provisioning (stage 3)

Mints a 24-hour, non-reusable, preauthorized [Tailscale](https://tailscale.com)
auth key for a card that's already flashed *and* partitioned (via stages 1–2
here, or any other tool), GPG-encrypts it onto the card's root filesystem
under a random per-card token, and installs a first-boot `systemd` service
that decrypts the key, runs `tailscale up`, then shreds the key/token and
removes itself — so nothing sensitive survives on the finished device. This
is a standalone action (the **Provision now** button) — it never runs
automatically during a Flash OS or Partition cards run.

Security model: the real Tailscale API credential never touches the card.
The per-card token does ride on the card (needed to decrypt at boot, since
the Pi never calls home) — a `dd` clone of the card copies it too, so this
is **not** anti-clone. Runtime protection is the key's non-reusable +
24-hour-expiry + tag ACLs.

**Station settings** (top of the Tailscale tab, station-wide — not part of
a saved profile):

| Field | Meaning |
|---|---|
| `mint_endpoint` | URL of a server that holds the real Tailscale API credential and mints keys on request — the station itself never sees the credential. Leave blank to use the fallback below instead. |
| `ts_apikey_file` | Local Tailscale API access token file (`tskey-api-...`), used directly via the Tailscale API when `mint_endpoint` is blank. Never leaves this station. |
| `tailnet` | Tailnet name, or `-` for "the tailnet this key belongs to" (default). |
| `tag` | ACL tag every minted key is scoped to (default `tag:production`). |
| `key_ttl_seconds` | Key expiry (default `86400` = 24h). |
| `ledger_endpoint` / `ledger_auth_token` | Optional — POST each issued key's token/id/reader to an external ledger for audit. Only used on the local-API-key fallback path; a configured `mint_endpoint` is assumed to record its own ledger row. |

Copy `tailscale_config.example.json` to `tailscale_config.json` to set these
outside the UI (gitignored, same pattern as `config.json`).

A **📒 View ledger** button shows the last 50 keys issued (timestamp,
device, hostname, token prefix, key id, status) — also mirrored into the
regular flash-history log's Detail column.

## Profiles (one profile, both stages)

One saved profile always carries **both** the flash config (stage 1) and the
partition layout (stage 2) together — select it once, it applies wherever
you are. **Save as** on either tab saves both halves under the same name;
selecting a profile applies both.

The header **⬆ / ⬇** icons export/import a single JSON file with up to
three top-level sections — any subset is valid:

```json
{
  "config": { "hostname": "...", "user": "...", "...": "..." },
  "partition": { "table": "mbr", "keep_existing": 2, "resize_last_kept_mib": 51200, "partitions": [ "..." ] },
  "tailscale": { "mint_endpoint": "...", "tag": "tag:production", "...": "..." }
}
```

Uploading a file with a `config` and/or `partition` section prompts for a
profile name and saves both together, the same as **Save as**. A
`tailscale` section (if present) is applied and saved straight to the
station-wide Tailscale settings — no profile name needed, since those
settings aren't per-profile.

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
| `server.py` | Web UI backend: device detection, parallel `dd` flashing, partitioning, Tailscale provisioning, profiles, history, config injection |
| `index.html` | Web UI frontend (all three stages) |
| `firstrun_gen.py` | Single shared implementation of the `firstrun.sh` template (hostname, user, SSH keys, Wi-Fi, static IP, timezone, keyboard) — used by `server.py`, `inject-config.sh`, and `flash-all.sh` so there's one place to trust, not three |
| `flash-all.sh` | CLI-only alternative using `rpi-imager --cli` |
| `inject-config.sh` | Re-inject config into an already-flashed card |
| `config.example.json` | Template — copy to `config.json` and edit |
| `profiles.example.json` | Sample named presets — copy to `profiles.json`, or just build them from the UI |
| `tailscale_config.example.json` | Template — copy to `tailscale_config.json` and edit (stage 3 station settings) |
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

## Troubleshooting

**"I reinstalled the `.deb`/rebuilt, but my change didn't take effect."**
`index.html` is read fresh from disk on every request, so a browser hard
refresh (`Ctrl+Shift+R`) is enough for frontend-only changes. `server.py` is
different: Python loads it once into memory when the server process starts,
and the packaged app's single-instance design (`packaging/piforge`) reuses
an already-running server across app relaunches rather than restarting it —
so a backend code change needs the actual process killed, not just the app
window closed:

```bash
sudo pkill -f /opt/piforge/server.py
pkill -f /usr/bin/piforge
piforge
```

**A card operation ended in "error".** Click **🔍 Details** on the card for
the full error (not the truncated one-line summary) — command, exit code,
and stderr/stdout for a failed subprocess, or a full traceback for anything
else — plus **⤓ Load** buttons for the server log
(`/tmp/piforge-server.log`) and that device's own flash log
(`/tmp/flash-<device>.log`), right in the same panel.

**Tailscale minting returns a 502.** That comes from the mint server itself
(if using `mint_endpoint`) rejecting or failing to reach the real Tailscale
API — check that server's own logs. A stored API key that was rotated after
the mint server's container was last started/recreated is the most common
cause; a `docker restart` doesn't reload `environment:` values, only a
recreate does (`docker compose up -d --force-recreate`, or the container's
`docker run` equivalent).

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

## Production master-card cloning

Use the top-bar disc icon to open **Make image from card** and save a fully
installed master. In Flash OS select **Custom built from PiForge**, choose its
local .img file (keep its matching .json beside it), and burn batches
of up to ten cards. Its partitions are preserved and the partition stage is
skipped. Each copy gets unique first-boot identity and can continue directly
to per-card Tailscale injection.
See [the production workflow](PRODUCTION.md) for preparation, Tailscale
enrollment, dashboard finalization and required physical-device checks.

### Fast burns and sharing (1.2.2)

PiForge saves a reusable, checksummed block map inside each image's .json file.
Older images generate it once on their first burn. Subsequent burns copy mapped
blocks without scanning source ext4 inodes; target filesystem checks remain.
Share .img + .json together or create a sparse compressed archive using:

```bash
python3 /opt/piforge/share_image.py /path/master.img /path/master.tar.gz
```

The recipient extracts the archive and selects the .img in the existing local
image picker. The block map travels with the image and is reused on their PC.
