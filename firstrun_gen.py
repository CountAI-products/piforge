#!/usr/bin/env python3
"""
Shared first-boot script generator — the one place that knows how to build
an Imager-style firstrun.sh (hostname, user, password, Wi-Fi, country,
timezone, SSH). Used directly by server.py, and via the CLI below by
inject-config.sh / flash-all.sh, so there is exactly one implementation to
trust instead of three copies drifting apart.

CLI usage:
    python3 firstrun_gen.py --config config.json --hostname pi1
    python3 firstrun_gen.py --config config.json --hostname pi1 --json '{"password":"x", ...}'

Prints the rendered firstrun.sh to stdout. Config keys (see config.example.json):
  user, password, wifi_ssid, wifi_password, wifi_country, timezone, keymap,
  enable_ssh. wifi_ssid may be empty/absent to skip Wi-Fi setup entirely
  (e.g. Ethernet-only boards).
"""

import argparse
import json
import re
import subprocess
import sys

FIRSTRUN_TEMPLATE = r"""#!/bin/bash
set +e
CURRENT_HOSTNAME=$(cat /etc/hostname | tr -d " \t\n\r")
if [ -f /usr/lib/raspberrypi-sys-mods/imager_custom ]; then
   /usr/lib/raspberrypi-sys-mods/imager_custom set_hostname '@@HOSTNAME@@'
else
   echo '@@HOSTNAME@@' >/etc/hostname
   sed -i "s/127.0.1.1.*$CURRENT_HOSTNAME/127.0.1.1\t@@HOSTNAME@@/g" /etc/hosts
fi
FIRSTUSER=$(getent passwd 1000 | cut -d: -f1)
FIRSTUSERHOME=$(getent passwd 1000 | cut -d: -f6)
@@SSH_BLOCK@@
if [ -f /usr/lib/userconf-pi/userconf ]; then
   /usr/lib/userconf-pi/userconf '@@USER@@' '@@HASH@@'
else
   echo "$FIRSTUSER:"'@@HASH@@' | chpasswd -e
   if [ "$FIRSTUSER" != "@@USER@@" ]; then
      usermod -l '@@USER@@' "$FIRSTUSER"
      usermod -m -d "/home/@@USER@@" '@@USER@@'
      groupmod -n '@@USER@@' "$FIRSTUSER"
      if grep -q "^autologin-user=" /etc/lightdm/lightdm.conf ; then
         sed /etc/lightdm/lightdm.conf -i -e "s/^autologin-user=.*/autologin-user=@@USER@@/"
      fi
      if [ -f /etc/systemd/system/getty@tty1.service.d/autologin.conf ]; then
         sed /etc/systemd/system/getty@tty1.service.d/autologin.conf -i -e "s/$FIRSTUSER/@@USER@@/"
      fi
      if [ -f /etc/sudoers.d/010_pi-nopasswd ]; then
         sed -i "s/^$FIRSTUSER /@@USER@@ /" /etc/sudoers.d/010_pi-nopasswd
      fi
   fi
fi
@@SSHKEY_BLOCK@@
@@WIFI_BLOCK@@
@@STATICIP_BLOCK@@
if [ -f /usr/lib/raspberrypi-sys-mods/imager_custom ]; then
   /usr/lib/raspberrypi-sys-mods/imager_custom set_keymap '@@KEYMAP@@'
   /usr/lib/raspberrypi-sys-mods/imager_custom set_timezone '@@TZ@@'
else
   rm -f /etc/localtime
   echo "@@TZ@@" >/etc/timezone
   dpkg-reconfigure -f noninteractive tzdata
fi
rm -f /boot/firstrun.sh
sed -i 's| systemd.run.*||g' /boot/cmdline.txt
exit 0
"""

SSH_BLOCK = """if [ -f /usr/lib/raspberrypi-sys-mods/imager_custom ]; then
   /usr/lib/raspberrypi-sys-mods/imager_custom enable_ssh
else
   systemctl enable ssh
fi"""

WIFI_BLOCK_TEMPLATE = """if [ -f /usr/lib/raspberrypi-sys-mods/imager_custom ]; then
   /usr/lib/raspberrypi-sys-mods/imager_custom set_wlan '@@SSID@@' '@@PSK@@' '@@COUNTRY@@'
else
cat >/etc/wpa_supplicant/wpa_supplicant.conf <<'WPAEOF'
country=@@COUNTRY@@
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
ap_scan=1
update_config=1
network={
	ssid="@@SSID@@"
	psk=@@PSK@@
}
WPAEOF
   chmod 600 /etc/wpa_supplicant/wpa_supplicant.conf
   rfkill unblock wifi
   for filename in /var/lib/systemd/rfkill/*:wlan ; do
       echo 0 > $filename
   done
fi"""

SSHKEY_BLOCK_TEMPLATE = """install -d -m 700 -o '@@USER@@' -g '@@USER@@' '/home/@@USER@@/.ssh'
cat > '/home/@@USER@@/.ssh/authorized_keys' <<'SSHKEYEOF'
@@KEYS@@
SSHKEYEOF
chmod 600 '/home/@@USER@@/.ssh/authorized_keys'
chown '@@USER@@:@@USER@@' '/home/@@USER@@/.ssh/authorized_keys'
@@DISABLE_PW_BLOCK@@"""

DISABLE_PW_BLOCK = """mkdir -p /etc/ssh/sshd_config.d
echo 'PasswordAuthentication no' > /etc/ssh/sshd_config.d/99-piforge.conf"""

STATICIP_BLOCK_TEMPLATE = """cat >> /etc/dhcpcd.conf <<'DHCPEOF'

interface @@IFACE@@
static ip_address=@@IP@@/@@CIDR@@
static routers=@@GATEWAY@@
static domain_name_servers=@@DNS@@
DHCPEOF"""


def sh_hash_password(password):
    return subprocess.run(["openssl", "passwd", "-6", password],
                          capture_output=True, text=True, check=True).stdout.strip()


def wifi_psk(ssid, password):
    out = subprocess.run(["wpa_passphrase", ssid, password],
                         capture_output=True, text=True).stdout
    m = re.search(r"^\s*psk=([0-9a-f]{64})$", out, re.M)
    return m.group(1) if m else password


def make_firstrun(cfg, hostname, static_ip=None):
    """cfg: dict with user, password, wifi_ssid, wifi_password, wifi_country,
    timezone, keymap, enable_ssh, ssh_authorized_key, disable_ssh_password,
    static_ip_cidr/gateway/dns/iface. Hashes/PSK are computed here if not
    already present as _hash/_psk (server.py precomputes them once for many
    cards). static_ip, if given, is this card's own address (server.py
    computes it per-card from static_ip_base + index)."""
    has_key = bool(cfg.get("ssh_authorized_key", "").strip())
    ssh_block = SSH_BLOCK if (cfg.get("enable_ssh", True) or has_key) else ""

    sshkey_block = ""
    if has_key:
        disable_pw = DISABLE_PW_BLOCK if cfg.get("disable_ssh_password") else ""
        sshkey_block = (SSHKEY_BLOCK_TEMPLATE
                        .replace("@@USER@@", cfg["user"])
                        .replace("@@KEYS@@", cfg["ssh_authorized_key"].strip())
                        .replace("@@DISABLE_PW_BLOCK@@", disable_pw))

    wifi_block = ""
    if cfg.get("wifi_ssid"):
        psk = cfg.get("_psk") or wifi_psk(cfg["wifi_ssid"], cfg.get("wifi_password", ""))
        wifi_block = (WIFI_BLOCK_TEMPLATE
                      .replace("@@SSID@@", cfg["wifi_ssid"])
                      .replace("@@PSK@@", psk)
                      .replace("@@COUNTRY@@", cfg.get("wifi_country") or "US"))

    staticip_block = ""
    ip = static_ip or cfg.get("_static_ip")
    if ip:
        staticip_block = (STATICIP_BLOCK_TEMPLATE
                          .replace("@@IFACE@@", cfg.get("static_ip_iface") or "eth0")
                          .replace("@@IP@@", ip)
                          .replace("@@CIDR@@", str(cfg.get("static_ip_cidr") or 24))
                          .replace("@@GATEWAY@@", cfg.get("static_ip_gateway") or "")
                          .replace("@@DNS@@", cfg.get("static_ip_dns")
                                   or cfg.get("static_ip_gateway") or "1.1.1.1"))

    password_hash = cfg.get("_hash") or sh_hash_password(cfg["password"])

    s = FIRSTRUN_TEMPLATE
    s = s.replace("@@SSH_BLOCK@@", ssh_block)
    s = s.replace("@@SSHKEY_BLOCK@@", sshkey_block)
    s = s.replace("@@WIFI_BLOCK@@", wifi_block)
    s = s.replace("@@STATICIP_BLOCK@@", staticip_block)
    for token, val in (
        ("@@HOSTNAME@@", hostname),
        ("@@USER@@", cfg["user"]),
        ("@@HASH@@", password_hash),
        ("@@TZ@@", cfg.get("timezone") or "UTC"),
        ("@@KEYMAP@@", cfg.get("keymap") or "us"),
    ):
        s = s.replace(token, val)
    return s


def compute_static_ip(base_ip, index):
    """base_ip + (index-1) on the last octet, e.g. 192.168.50.10 + index 3
    -> 192.168.50.12. Same 1-based numbering scheme as hostnames, so card 1
    gets the base address itself. Not meant for batches over ~200 cards
    (no rollover past .254)."""
    octets = base_ip.strip().split(".")
    if len(octets) != 4:
        raise ValueError(f"static_ip_base is not a dotted IPv4 address: {base_ip!r}")
    last = int(octets[3]) + (index - 1)
    if not (0 <= last <= 255):
        raise ValueError(f"static IP offset out of range for base {base_ip!r} at index {index}")
    octets[3] = str(last)
    return ".".join(octets)


def _main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="path to config.json")
    ap.add_argument("--hostname", required=True)
    ap.add_argument("--index", type=int, default=1,
                    help="1-based card index, used for static_ip_base offset (default 1)")
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)
    if not cfg.get("password"):
        print("ERROR: config has no password set", file=sys.stderr)
        sys.exit(1)

    static_ip = None
    if cfg.get("static_ip_base"):
        static_ip = compute_static_ip(cfg["static_ip_base"], args.index)

    sys.stdout.write(make_firstrun(cfg, args.hostname, static_ip=static_ip))


if __name__ == "__main__":
    _main()


def make_identity_firstrun(hostname):
    """Clone identity runs before normal network services, preserving software."""
    if not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", hostname or ""):
        raise ValueError("Hostname must be 1-63 letters, digits or hyphens")
    return r"""#!/bin/bash
set -euo pipefail
printf '%s\n' '__HOST__' > /etc/hostname
hostname '__HOST__'
if grep -q '^127.0.1.1' /etc/hosts; then
    sed -i 's/^127.0.1.1.*/127.0.1.1\t__HOST__/' /etc/hosts
else
    printf '127.0.1.1\t__HOST__\n' >> /etc/hosts
fi
systemd-machine-id-setup
mkdir -p /var/lib/dbus
ln -sf /etc/machine-id /var/lib/dbus/machine-id
ssh-keygen -A
for boot in /boot/firmware /boot; do
    if [ -f "$boot/cmdline.txt" ]; then
        mount -o remount,rw "$boot"
        sed -i 's/ systemd.run[^ ]*//g; s/ systemd.unit=kernel-command-line.target//g' "$boot/cmdline.txt"
        rm -f "$boot/firstrun.sh"
    fi
done
sync
""".replace('__HOST__', hostname)
