#!/usr/bin/env bash
# A new Ubuntu 24.04 server from the template, like ordering a VPS. Run in the Proxmox shell:
#   bash proxmox-new-server.sh <id> <name> <address/24> [memory MB] [disk GB]
# Example: bash proxmox-new-server.sh 101 villa-server-1 192.168.200.61/24 4096 40
# Pick an address on YOUR network that nothing uses (the script checks), high in the range
# (e.g. .61) so it does not clash with what the router hands out.
# To start over: qm stop 101 && qm destroy 101 --purge, then run this again.
set -euo pipefail
ID="${1:?id, e.g. 101}"; NAME="${2:?name, e.g. villa-server-1}"
ADDR="${3:?address, e.g. 192.168.200.61/24}"
MEM="${4:-4096}"; DISK="${5:-40}"
IP="${ADDR%/*}"
GW=$(ip route | awk '/^default/ {print $3; exit}')
if ping -c 2 -W 1 "$IP" >/dev/null 2>&1; then
  echo "Address $IP answers already: another device uses it. Pick another one."; exit 1
fi
echo "==> Copying the template (1-3 minutes, do not close this window)"
qm clone 9000 "$ID" --name "$NAME" --full 1
qm set "$ID" --memory "$MEM" --onboot 1 --ipconfig0 "ip=$ADDR,gw=$GW" --nameserver "$GW"
qm resize "$ID" scsi0 "${DISK}G"
qm start "$ID"
echo "Server $NAME (VM $ID) is starting; give it 2 minutes (first-boot security updates)."
echo "Then connect with Termius/PuTTY:  $IP, user villaos, your password."
