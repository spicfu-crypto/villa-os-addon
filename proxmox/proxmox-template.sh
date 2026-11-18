#!/usr/bin/env bash
# Run ONCE in the Proxmox shell (as root). Makes the Ubuntu Server 24.04 LTS template
# (VM 9000) that every new server is copied from, in under a minute.
#   bash proxmox-template.sh                 asks for the password of user "villaos"
#   bash proxmox-template.sh "ssh-ed25519 …"   optional: also allow this SSH key
# Login with a password over SSH is turned ON (Ubuntu cloud images turn it off), so you can
# use Termius/PuTTY with user villaos and your password. Only for servers on your own LAN.
set -euo pipefail
KEY="${1:-}"
ID=9000
STORAGE="${STORAGE:-local-lvm}"
IMG=/var/lib/vz/template/noble-server-cloudimg-amd64.img
qm status $ID >/dev/null 2>&1 && { echo "VM $ID already exists (the template is already there)."; exit 0; }

while :; do
  read -rsp "Password for user villaos on the new servers (letters and digits, 10+): " PW; echo
  [ "${#PW}" -ge 10 ] && [[ "$PW" =~ ^[A-Za-z0-9._-]+$ ]] && break
  echo "Use at least 10 characters, only Latin letters, digits, . _ - (keyboards differ in consoles)."
done

echo "==> Allow snippets on 'local' and add the password-login snippet"
pvesm set local --content iso,vztmpl,backup,snippets
mkdir -p /var/lib/vz/snippets
cat > /var/lib/vz/snippets/villaos-pwauth.yaml <<'YAML'
#cloud-config
ssh_pwauth: true
runcmd:
  - sed -i 's/^PasswordAuthentication no/PasswordAuthentication yes/' /etc/ssh/sshd_config.d/*.conf
  - systemctl restart ssh
YAML

echo "==> Download the official Ubuntu 24.04 cloud image (~600 MB)"
mkdir -p /var/lib/vz/template
wget -q --show-progress -O "$IMG" https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img

echo "==> Create the template"
qm create $ID --name ubuntu-2404-template --ostype l26 --cpu host --cores 2 --memory 4096 \
  --net0 virtio,bridge=vmbr0 --scsihw virtio-scsi-single
qm set $ID --scsi0 "$STORAGE:0,import-from=$IMG,discard=on,ssd=1"
qm set $ID --ide2 "$STORAGE:cloudinit" --boot order=scsi0 --serial0 socket --vga std
qm set $ID --ciuser villaos --cipassword "$PW" --ipconfig0 ip=dhcp --ciupgrade 1 \
  --cicustom vendor=local:snippets/villaos-pwauth.yaml
if [ -n "$KEY" ]; then
  printf '%s\n' "$KEY" > /root/villaos-ssh.pub
  qm set $ID --sshkeys /root/villaos-ssh.pub
fi
qm template $ID
echo "Done: template $ID. New server: bash proxmox-new-server.sh 101 villa-server-1 <free address>/24"
