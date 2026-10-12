#!/usr/bin/env bash
# Run AFTER device registration and the existing post-install script/reboot.
# Pass the actual required systemd service names from your production setup.
set -euo pipefail
if [ "$EUID" -ne 0 ]; then echo 'Run with sudo.' >&2; exit 1; fi
if [ "$#" -eq 0 ]; then echo 'Usage: sudo bash dashboard-finalize.sh watchdog.service <application.service> [...]' >&2; exit 1; fi
serial=$(awk '/^Serial[[:space:]]*:/ {print $3}' /proc/cpuinfo)
[ -n "$serial" ] || { echo 'Cannot identify this physical Pi.' >&2; exit 1; }
for service in "$@"; do
    [[ "$service" =~ ^[a-zA-Z0-9@_.:-]+$ ]] || { echo 'Invalid service name.' >&2; exit 1; }
    systemctl is-active --quiet "$service" || { echo "FAIL: $service is inactive" >&2; exit 1; }
done
hostname=$(hostname)
machine_id=$(cat /etc/machine-id)
[ "${#machine_id}" -eq 32 ] || { echo 'Invalid machine identity.' >&2; exit 1; }
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
install -d -m 0755 /var/lib/piforge
printf 'serial=%s\nhostname=%s\nmachine_id=%s\nverified_at=%s\n' "$serial" "$hostname" "$machine_id" "$(date -u +%FT%TZ)" > /var/lib/piforge/device-verified.tmp
mv /var/lib/piforge/device-verified.tmp /var/lib/piforge/device-verified
printf 'PASS: %s (%s); required services active.\n' "$hostname" "$serial"
