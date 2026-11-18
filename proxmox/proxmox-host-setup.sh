#!/usr/bin/env bash
# Run ONCE on a fresh Proxmox VE 9 (as root, in the console or over SSH), right after install.
#   bash proxmox-host-setup.sh            updates without subscription + laptop lid
#   bash proxmox-host-setup.sh /dev/sdb   ... and turn this EMPTY disk into the backup disk
# What it does, all of it what we did by hand on the first laptop:
#  1. Free updates: turns off the enterprise (paid) sources, turns on "no-subscription".
#  2. Laptop: closing the lid does not suspend the server.
#  3. Optional: formats the given EMPTY disk, mounts it at /mnt/backup, adds it to Proxmox
#     as "backup" and backs up every VM each night at 03:00 (7 daily + 4 weekly kept).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run as root."; exit 1; }
DISK="${1:-}"

echo "==> 1. Updates without subscription"
for f in /etc/apt/sources.list.d/pve-enterprise.sources /etc/apt/sources.list.d/ceph.sources; do
  [ -f "$f" ] && ! grep -q '^Enabled: no' "$f" && sed -i '1i Enabled: no' "$f"
done
cat > /etc/apt/sources.list.d/proxmox.sources <<'SRC'
Types: deb
URIs: http://download.proxmox.com/debian/pve
Suites: trixie
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
SRC
apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get full-upgrade -y -q

echo "==> 2. Lid closed = keep running"
mkdir -p /etc/systemd/logind.conf.d
printf '[Login]\nHandleLidSwitch=ignore\nHandleLidSwitchExternalPower=ignore\nHandleLidSwitchDocked=ignore\n' \
  > /etc/systemd/logind.conf.d/lid.conf
systemctl restart systemd-logind

if [ -n "$DISK" ]; then
  echo "==> 3. Backup disk $DISK"
  [ -b "$DISK" ] || { echo "$DISK is not a disk."; exit 1; }
  if lsblk -no MOUNTPOINTS "$DISK" | grep -q . || [ "$(lsblk -no NAME "$DISK" | wc -l)" -gt 1 ]; then
    echo "$DISK has partitions or is in use. Refusing to erase it. Check with: lsblk"; exit 1
  fi
  lsblk -o NAME,SIZE,MODEL "$DISK"
  read -rp "Erase $DISK and use it for backups? Type YES: " ok
  [ "$ok" = YES ] || { echo "Stopped, nothing erased."; exit 1; }
  wipefs -a "$DISK"
  mkfs.ext4 -F -L backup "$DISK"
  mkdir -p /mnt/backup
  grep -q 'LABEL=backup' /etc/fstab || echo 'LABEL=backup /mnt/backup ext4 defaults,nofail 0 2' >> /etc/fstab
  systemctl daemon-reload
  mount /mnt/backup
  pvesm status | awk '{print $1}' | grep -qx backup \
    || pvesm add dir backup --path /mnt/backup --content backup,iso,vztmpl --is_mountpoint yes
  pvesh get /cluster/backup --output-format json | grep -q '"storage":"backup"' \
    || pvesh create /cluster/backup --schedule "03:00" --storage backup --all 1 --mode snapshot \
         --compress zstd --prune-backups keep-daily=7,keep-weekly=4 --enabled 1
  df -h /mnt/backup
fi
echo "Done. Reboot once to load the updates: reboot"
