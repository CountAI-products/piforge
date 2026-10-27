# 100 preinstalled cards in three hours

Prepare the master before the production window. With ten readers, ten batches
must average less than 18 minutes including verification, provisioning and swaps.
Aim for 15 minutes per batch; no throughput claim has been measured yet.

## Master preparation

On a real Pi with the correct partition layout, run super3nova.sh, allow its
reboot, then run super3nova_post_installation.sh and verify the application.
Remove production records, camera captures, licenses and device registration
from the template according to your application requirements. Do not enable
overlayroot yet. Do not enroll the master in Tailscale. Shut down cleanly.

On the cloning station install Python 3, e2fsprogs 1.47.2+, util-linux,
parted and udev. PiForge refuses cloning with e2fsprogs 1.47.0 or 1.47.1
because those versions contain unsafe `orphan_file` recovery behavior.
Run `sudo python3 server.py` and open http://127.0.0.1:47823/.
Click the top-bar disc icon to open Make image from card. Select only the
master reader, enter its image name, then click Make image from selected card.
Currently only MBR/DOS layouts are supported; partition 1 must be the FAT boot
partition and partition 2 the OS root. Unsupported layouts are not converted.
All data partitions are preserved. ext4 copies use e2image used-block copying;
other filesystems are copied in full. Only the last ext4 partition is shrunk
and regrown, so earlier large partitions still impose a minimum card capacity.
The original master is not resized or changed. Every cloned ext4 partition is
checked, has `orphan_file` disabled while unmounted, and is checked again. This
keeps Bookworm's boot-time e2fsck 1.47.0 away from its known recovery failure
after an unexpected power cut. Clones receive filesystem checks, not a
byte-for-byte readback or application acceptance test.

## Production batches

Select up to ten blank target cards. In Flash OS choose Custom built from
PiForge, then Choose image from local folder. Select the .img file; its matching
.json metadata must be in the same folder. The station validates the metadata
and registers the image without copying or uploading its contents. Keep the
source file available until the batch finishes. Saved station images also
appear directly in the OS image selector. Set a hostname prefix and starting number: 1, 11, 21 ... 91.
The usual START button writes the image. The Partition cards stage is skipped
and disabled because the saved image includes its partition layout. Confirm the actual target readers
before writing. The operation erases selected targets. Do not remove cards
until their job shows done. Failed cards require a retry, not deployment.

Each target is prepared offline: copied machine ID, SSH host keys, Tailscale
state and pending Tailscale enrollment material are cleared. First boot creates
its hostname and fresh identity before regular network services. The original
post-install marker is removed from both root and persistent-home layouts.
Application registration and application secrets require your own template
cleanup; PiForge does not infer them.

Leave Inject a unique Tailscale key after writing checked to continue directly
to injection after each successful write. Configure station Tailscale settings
first. Alternatively, uncheck it and use the existing Tailscale tab afterward.
This issues a separate single-use key per card; do not clone an
enrolled or provisioned card as another production master. Default keys expire
in 24 hours. Software, including Tailscale, should already be in the master so
first boot does not repeat heavy installation.

Boot two trial copies and check different hostnames, machine IDs, SSH keys and
Tailscale nodes. Test the actual camera, database and application. Then measure
one ten-reader batch on the real USB controllers before making the remaining 90.

## Actual-device dashboard step

Register the device and apply its application-specific settings from the admin
dashboard. Run your existing post-install script on each actual Pi to apply its
EEPROM settings and watchdog configuration. Allow the required reboot. Removing
the cloned .post_install_done marker makes this initial run possible.

After reconnecting, run dashboard-finalize.sh with watchdog.service and your
actual application service names. It checks each named service and records the
physical Pi serial, hostname and machine ID only when all pass. It is rerunnable
and does not skip checks based on a copied marker. Its result does not validate
camera output, database contents, container health or EEPROM readback: retain
those checks in your dashboard before declaring the device ready or locking
its filesystem. The existing post-install script can write its own marker even
when EEPROM staging fails; do not treat that marker as a readiness signal.

This repository adds the station workflow and a service-verification helper.
It does not change or integrate your external admin dashboard.

## Fast reusable block maps (1.2.2)

New master images include a block_map in their existing .json companion file.
An older image creates this map once before its first burn. Concurrent burns
share the same map build through a file lock. Building reads the ext4 block-allocation bitmaps and sparse extents, includes
zero-valued allocated blocks needed on reused cards, and calculates SHA-256
checksums once; burning writes those extents and
checks their hashes while copying, without running e2image on the source.
Target filesystem checks and resizing still run. Copy progress is the mapped
bytes across the whole image, followed by flushing, resize and verification.
The sparse map may include allocated zero blocks, so mapped bytes can exceed
actual nonzero data. On filesystems without sparse extent support the one-time
map covers the full file. Never modify a source image or its metadata during a
batch. A checksum failure requires recreating the image/map, not ignoring it.

## Share an image with another person

Share the .img and .json together. The JSON includes the reusable map and hashes;
no separate third file is needed. To keep empty space small during transfer:

```bash
python3 /opt/piforge/share_image.py /home/USER/rpi-images/clones/master.img /path/master.tar.gz
```

This makes the map first if needed, then creates a sparse compressed archive
with only the image and metadata. It excludes station settings and enrollment
files outside the image. The master itself must still be cleaned of production
data, registrations and credentials before it is shared.

On the receiving Ubuntu PC:

```bash
tar -xzf master.tar.gz
```

In PiForge select Custom built from PiForge and browse to the extracted .img.
Keep .json beside it. The included map is reused on that PC without another
source scan. Writable metadata-folder access is needed for the map lock.


## Authenticated operators (1.3.0)

Deploy the matching tailscale_auth server changes before installing this client.
Configure the server /mint endpoint and its station authorization token in the
Tailscale settings. Use Sign in / Register in PiForge's top bar. The username is
server-confirmed, not a typed installer label. Registration creates a server-side
account and signs in; login is required before card burning or key injection.
A station restart or eight-hour session expiry requires login again. Credentials
are not persisted locally and accounts/sessions do not enter the card image.
Operator changes are blocked while a card operation is active.

The server records the operator, key issuance, exact-node join report, IP and
completed preparation duration. The first-boot service retries failed join
reports without exposing operator credentials. Server changes have not been
deployed automatically; the existing live server must be upgraded separately.


## Tailscale SSH and subnet routes (1.3.1)

First-boot authentication now runs tailscale up with --ssh=true and
--accept-routes=true, alongside the per-card auth key, hostname and production
tag. The key is still minted on the ledger server; these are device settings
applied when the Pi joins, not properties of the auth key itself.
Tailscale SSH requires an SSH access rule permitting the intended operators to
reach tag:production and the chosen Linux users. Accept-routes consumes approved
subnet routes advertised by other tailnet devices; it does not advertise routes
from this Pi. For a previously enrolled Pi, use:

```bash
sudo tailscale set --ssh=true --accept-routes=true
```

Existing burned cards are not modified by upgrading the station. Apply the above
command on those Pis, or re-inject the updated first-boot provisioning on a card
before its initial boot. Physical SSH and subnet connectivity remain to be tested
with your actual tailnet access policy.


## Create a shareable file from the desktop (1.3.2)

Click the top-bar package icon (Create shareable image), choose a saved image,
and click Create shareable file. PiForge prepares the map and compresses the
image and matching JSON into a single sparse .tar.gz archive. Status and elapsed
time update during preparation/compression; compression has no percentage ETA.
The resulting path is shown and can be copied. Archives are stored in
rpi-images/clones/shared. The recipient extracts the archive and selects the
.img through Custom built from PiForge; the companion JSON stays alongside it.
Archive creation does not send files to another person automatically.
