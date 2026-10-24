#!/usr/bin/env bash
# setup.sh -- turns a fresh Oracle Cloud Ubuntu 24.04 instance (Ampere A1
# arm64 or AMD x86_64) into the MyOtherApp box. Safe to run again: every step
# checks before it changes anything, and says what it did.
#
#   sudo bash ops/server/setup.sh --admin-cidr 203.0.113.7/32 \
#     [--admin-cidr 198.51.100.0/24] [--ssh-from-anywhere] [--tailscale] \
#     [--data-device /dev/oracleoci/oraclevdb [--format-data]] \
#     [--deploy-key-file ci-deploy.pub] [--swap-gb 4] [--timezone Asia/Kolkata] \
#     [--auto-reboot 03:30] [--staging] [--update-stack] [--hostname moa-prod]
#
# What it does (docs/MOVE_TO_ORACLE.md, step 2, has the why):
#   1. timezone, base packages, sysctl, journald size limit
#   2. the block volume (if given) at /srv/moa-data: Docker's data, backups
#      and GeoLite2 live there, so they survive the instance
#   3. a swapfile (4 GB)
#   4. the host firewall, the Oracle way: /etc/iptables/rules.v4 (NOT ufw --
#      Oracle documents that ufw can leave an instance unable to boot, by
#      dropping the rules its boot and block volumes need). SSH only from
#      --admin-cidr (and Tailscale), 80/443 from anywhere; Oracle's own
#      InstanceServices rules are left exactly as they are
#   5. Docker from Docker's apt repository, with log rotation and
#      live-restore, its data on the block volume
#   6. fail2ban for SSH, unattended security updates, SSH keys only
#   7. the deploy user `moa-deploy`, whose key can only run deploy-gate.sh
#   8. /srv/moa with the stack's files (from this directory), and the daily
#      backup timer when ops/backup is there
#   9. optionally Tailscale, so CI and admins reach SSH without opening it
#
# It never writes /srv/moa/.env, env/*.env, certs/ or secrets/: those are
# copied in by hand (the runbook says how).
# `a && say ok || say problem`: the middle only prints, so it cannot fail.
# shellcheck disable=SC2015
set -Eeuo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
MOA_DIR=/srv/moa
DATA_DIR=/srv/moa-data
ADMIN_CIDRS=()
SSH_ANYWHERE=no
TAILSCALE=no
DATA_DEVICE=""
FORMAT_DATA=no
DEPLOY_KEY_FILE=""
SWAP_GB=4
TIMEZONE=Asia/Kolkata
AUTO_REBOOT=""
ROLE=production
UPDATE_STACK=no
NEW_HOSTNAME=""
FORCE_FIREWALL=no

say() { printf '   %s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
warn() { printf '   WARNING: %s\n' "$*" >&2; }
die() { printf 'setup.sh: %s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'
  exit 64
}

while [ $# -gt 0 ]; do
  case "$1" in
    --admin-cidr) ADMIN_CIDRS+=("${2:?}"); shift ;;
    --ssh-from-anywhere) SSH_ANYWHERE=yes ;;
    --tailscale) TAILSCALE=yes ;;
    --data-device) DATA_DEVICE="${2:?}"; shift ;;
    --format-data) FORMAT_DATA=yes ;;
    --deploy-key-file) DEPLOY_KEY_FILE="${2:?}"; shift ;;
    --swap-gb) SWAP_GB="${2:?}"; shift ;;
    --timezone) TIMEZONE="${2:?}"; shift ;;
    --auto-reboot) AUTO_REBOOT="${2:?}"; shift ;;
    --staging) ROLE=staging ;;
    --update-stack) UPDATE_STACK=yes ;;
    --hostname) NEW_HOSTNAME="${2:?}"; shift ;;
    --force-firewall) FORCE_FIREWALL=yes ;;
    -h | --help) usage ;;
    *) die "unknown option: $1 (--help)" ;;
  esac
  shift
done

[ "$(id -u)" = "0" ] || die "run as root: sudo bash $0 ..."
# shellcheck disable=SC1091
. /etc/os-release
[ "${ID:-}" = "ubuntu" ] || die "this is for Ubuntu (found ${ID:-unknown})"
[ "${VERSION_ID:-}" = "24.04" ] || warn "written for Ubuntu 24.04; this is ${VERSION_ID:-unknown}"
[[ "$SWAP_GB" =~ ^[0-9]{1,2}$ ]] || die "--swap-gb takes a whole number of GB"
[ -z "$AUTO_REBOOT" ] || [[ "$AUTO_REBOOT" =~ ^[0-2][0-9]:[0-5][0-9]$ ]] || die "--auto-reboot takes HH:MM"
if [ "${#ADMIN_CIDRS[@]}" -eq 0 ] && [ "$SSH_ANYWHERE" = "no" ] && [ "$TAILSCALE" = "no" ]; then
  die "say who may SSH in: --admin-cidr <your-ip>/32 (repeatable), --tailscale, or --ssh-from-anywhere"
fi
for cidr in "${ADMIN_CIDRS[@]}"; do
  python3 -c 'import ipaddress, sys; ipaddress.ip_network(sys.argv[1], strict=False)' "$cidr" 2> /dev/null ||
    die "--admin-cidr $cidr is not an address or network"
done

export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
ARCH="$(dpkg --print-architecture)"
say "Ubuntu ${VERSION_ID:-?} on $ARCH ($(nproc) cores, $(free -g | awk '/^Mem:/ {print $2}') GB), role: $ROLE"

# Writes $2 to file $1 only if it differs; returns 0 when it changed.
write_if_changed() {
  local file="$1" content="$2" mode="${3:-644}"
  if [ -f "$file" ] && [ "$(cat "$file")" = "$content" ]; then
    return 1
  fi
  mkdir -p "$(dirname "$file")"
  printf '%s\n' "$content" > "$file.moa-new"
  chmod "$mode" "$file.moa-new"
  mv -f "$file.moa-new" "$file"
  return 0
}

# ─── 1. Basics ───────────────────────────────────────────────────────────────
step "1. Timezone, packages, kernel settings, journald"
if [ "$(timedatectl show -p Timezone --value)" != "$TIMEZONE" ]; then
  timedatectl set-timezone "$TIMEZONE"
  say "timezone set to $TIMEZONE"
else
  say "timezone already $TIMEZONE"
fi
if [ -n "$NEW_HOSTNAME" ] && [ "$(hostnamectl --static)" != "$NEW_HOSTNAME" ]; then
  hostnamectl set-hostname "$NEW_HOSTNAME"
  say "hostname set to $NEW_HOSTNAME"
fi

# iptables-persistent asks on install whether to save the current rules.
echo "iptables-persistent iptables-persistent/autosave_v4 boolean false" | debconf-set-selections
echo "iptables-persistent iptables-persistent/autosave_v6 boolean false" | debconf-set-selections
apt-get update -q
apt-get install -y -q --no-install-recommends \
  ca-certificates curl gnupg jq rsync python3 util-linux \
  iptables iptables-persistent netfilter-persistent \
  fail2ban python3-systemd unattended-upgrades apt-listchanges
say "base packages present"

if write_if_changed /etc/sysctl.d/60-moa.conf "# MyOtherApp (ops/server/setup.sh)
# Swap only under real pressure: Postgres and the apps should stay in memory.
vm.swappiness = 10
# Room for bursts of new connections (Caddy, the live room's streams).
net.core.somaxconn = 4096"; then
  sysctl --system > /dev/null
  say "sysctl settings applied"
else
  say "sysctl settings already in place"
fi

if write_if_changed /etc/systemd/journald.conf.d/60-moa.conf "[Journal]
SystemMaxUse=500M
MaxRetentionSec=1month"; then
  systemctl restart systemd-journald
  say "journald capped at 500 MB"
else
  say "journald cap already in place"
fi

# ─── 2. Block volume ─────────────────────────────────────────────────────────
step "2. Data volume at $DATA_DIR"
if [ -n "$DATA_DEVICE" ]; then
  [ -b "$DATA_DEVICE" ] || die "$DATA_DEVICE is not a block device (attach the volume as paravirtualized, then use /dev/oracleoci/oraclevdb)"
  dev="$(readlink -f "$DATA_DEVICE")"
  if findmnt -rn -S "$dev" > /dev/null; then
    say "$DATA_DEVICE is mounted at $(findmnt -rn -S "$dev" -o TARGET | head -n 1)"
  else
    fstype="$(blkid -o value -s TYPE "$dev" || true)"
    if [ -z "$fstype" ]; then
      if lsblk -nr -o NAME "$dev" | tail -n +2 | grep -q .; then
        die "$DATA_DEVICE has partitions but no filesystem on the whole disk: refusing to touch it"
      fi
      [ "$FORMAT_DATA" = "yes" ] || die "$DATA_DEVICE has no filesystem. If it is the new, empty data volume, run again with --format-data"
      mkfs.ext4 -q -L moa-data "$dev"
      fstype=ext4
      say "formatted $DATA_DEVICE as ext4 (label moa-data)"
    elif [ "$fstype" != "ext4" ] && [ "$fstype" != "xfs" ]; then
      die "$DATA_DEVICE holds $fstype; expected ext4 or xfs"
    fi
    uuid="$(blkid -o value -s UUID "$dev")"
    mkdir -p "$DATA_DIR"
    if ! grep -q "UUID=$uuid" /etc/fstab; then
      # _netdev and nofail: Oracle's advice for attached volumes, so a
      # missing volume never stops the boot (Docker then refuses to start
      # rather than filling the boot volume: see docker's unit below).
      echo "UUID=$uuid $DATA_DIR $fstype defaults,_netdev,nofail 0 2" >> /etc/fstab
      systemctl daemon-reload
    fi
    mount "$DATA_DIR"
    say "mounted $DATA_DEVICE at $DATA_DIR"
  fi
  install -d -m 711 "$DATA_DIR/docker"
  install -d -m 700 "$DATA_DIR/backups"
  install -d -m 755 "$DATA_DIR/geoip"
  install -d -m 700 /var/backups/myotherapp
  install -d -m 755 /var/lib/myotherapp/geoip
  for pair in "$DATA_DIR/backups /var/backups/myotherapp" "$DATA_DIR/geoip /var/lib/myotherapp/geoip"; do
    read -r src dst <<< "$pair"
    if ! grep -qE "^$src $dst none bind" /etc/fstab; then
      echo "$src $dst none bind,nofail,x-systemd.requires-mounts-for=$DATA_DIR 0 0" >> /etc/fstab
      systemctl daemon-reload
    fi
    findmnt -rn "$dst" > /dev/null || mount "$dst"
  done
  say "backups and GeoLite2 directories live on the data volume"
else
  warn "no --data-device: Docker's data and the backups stay on the boot volume"
  install -d -m 700 /var/backups/myotherapp
  install -d -m 755 /var/lib/myotherapp/geoip
fi

# ─── 3. Swap ─────────────────────────────────────────────────────────────────
step "3. Swap (${SWAP_GB} GB)"
if [ "$SWAP_GB" = "0" ]; then
  say "skipped (--swap-gb 0)"
elif swapon --show=NAME --noheadings | grep -q .; then
  say "swap already on: $(swapon --show=NAME,SIZE --noheadings | tr -s ' ' | tr '\n' ' ')"
else
  if [ ! -f /swapfile ]; then
    fallocate -l "${SWAP_GB}G" /swapfile 2> /dev/null || dd if=/dev/zero of=/swapfile bs=1M count=$((SWAP_GB * 1024)) status=none
    chmod 600 /swapfile
    mkswap /swapfile > /dev/null
  fi
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  say "swapfile of ${SWAP_GB} GB on"
fi

# ─── 4. Firewall ─────────────────────────────────────────────────────────────
step "4. Host firewall (/etc/iptables/rules.v4; Oracle's rules kept)"
if command -v ufw > /dev/null && ufw status 2> /dev/null | grep -q "Status: active"; then
  warn "ufw is active: Oracle documents it can stop an instance from booting. Disable it (sudo ufw disable) and run this again."
  die "ufw is active"
fi
# Who is running this, so it cannot lock them out by mistake.
me="${SSH_CLIENT%% *}"
if [ -n "$me" ] && [ "$SSH_ANYWHERE" = "no" ] && [ "$FORCE_FIREWALL" = "no" ]; then
  covered="$(python3 - "$me" "${ADMIN_CIDRS[@]}" << 'PY'
import ipaddress, sys
me = ipaddress.ip_address(sys.argv[1])
print("yes" if any(me in ipaddress.ip_network(c, strict=False) for c in sys.argv[2:]) else "no")
PY
)"
  if [ "$covered" != "yes" ] && [ "$TAILSCALE" = "no" ]; then
    die "you are connected from $me, which no --admin-cidr covers: this would lock you out of SSH. Add --admin-cidr $me/32 (or --force-firewall if you know better)"
  fi
fi

block="# BEGIN moa (ops/server/setup.sh manages this block; run it again to change it)"
if [ "$SSH_ANYWHERE" = "yes" ]; then
  block+=$'\n'"-A INPUT -p tcp -m state --state NEW -m tcp --dport 22 -j ACCEPT"
else
  for cidr in "${ADMIN_CIDRS[@]}"; do
    case "$cidr" in *:*) continue ;; esac
    block+=$'\n'"-A INPUT -s $cidr -p tcp -m state --state NEW -m tcp --dport 22 -j ACCEPT"
  done
fi
if [ "$TAILSCALE" = "yes" ]; then
  block+=$'\n'"-A INPUT -i tailscale0 -p tcp -m state --state NEW -m tcp --dport 22 -j ACCEPT"
  block+=$'\n'"-A INPUT -p udp -m udp --dport 41641 -j ACCEPT"
fi
block+=$'\n'"-A INPUT -p tcp -m state --state NEW -m multiport --dports 80,443 -j ACCEPT"
block+=$'\n'"# END moa"

rules=/etc/iptables/rules.v4
install -d -m 755 /etc/iptables
current="$(cat "$rules" 2> /dev/null || true)"
new="$(BLOCK="$block" python3 - "$rules" << 'PY'
import os, re, sys
path = sys.argv[1]
block = os.environ["BLOCK"]
try:
    text = open(path).read()
except FileNotFoundError:
    text = ""
# Drop an earlier copy of our block.
text = re.sub(r"# BEGIN moa.*?# END moa\n?", "", text, flags=re.S)
oracle_ssh = "-A INPUT -p tcp -m state --state NEW -m tcp --dport 22 -j ACCEPT"
lines = text.splitlines()
lines = [("# moa: SSH is allowed in the block below instead: " + l) if l.strip() == oracle_ssh else l for l in lines]
reject = next((i for i, l in enumerate(lines) if re.match(r"-A INPUT -j REJECT", l.strip())), None)
if reject is not None:
    lines[reject:reject] = block.splitlines()
    print("\n".join(lines))
else:
    # Not Oracle's file: a small policy of our own.
    print("\n".join([
        "*filter", ":INPUT DROP [0:0]", ":FORWARD ACCEPT [0:0]", ":OUTPUT ACCEPT [0:0]",
        "-A INPUT -m state --state RELATED,ESTABLISHED -j ACCEPT",
        "-A INPUT -i lo -j ACCEPT", "-A INPUT -p icmp -j ACCEPT",
        *block.splitlines(), "COMMIT",
    ]))
PY
)"
if [ "$new" = "$current" ]; then
  say "rules.v4 already as wanted"
else
  printf '%s\n' "$new" > "$rules.moa-new"
  iptables-restore --test < "$rules.moa-new" || die "the new rules.v4 does not parse; nothing was changed ($rules.moa-new)"
  [ -f "$rules" ] && cp -p "$rules" "$rules.before-moa-$(date +%Y%m%d%H%M%S)"
  mv -f "$rules.moa-new" "$rules"
  iptables-restore < "$rules"
  say "rules.v4 written and applied: SSH from ${ADMIN_CIDRS[*]:-nowhere}$([ "$SSH_ANYWHERE" = yes ] && echo ' + anywhere')$([ "$TAILSCALE" = yes ] && echo ' + tailscale0'), 80/443 from anywhere"
  # A restore replaces every chain, Docker's and fail2ban's too: each
  # re-creates its own when restarted.
  if systemctl is-active --quiet docker; then
    systemctl restart docker
    say "docker restarted to put its own rules back (containers keep running: live-restore)"
  fi
  if systemctl is-active --quiet fail2ban; then
    systemctl restart fail2ban
  fi
fi
systemctl enable --quiet netfilter-persistent
say "IPv6: rules.v6 left as the image has it; the OCI security list filters IPv6 (the VCN has none unless enabled)"

# ─── 5. Docker ───────────────────────────────────────────────────────────────
step "5. Docker"
daemon='{
  "log-driver": "local",
  "log-opts": { "max-size": "20m", "max-file": "5" },
  "live-restore": true'
if [ -n "$DATA_DEVICE" ]; then
  daemon+=',
  "data-root": "'"$DATA_DIR"'/docker"'
fi
daemon+='
}'
if command -v dockerd > /dev/null && [ -n "$DATA_DEVICE" ] && [ -f /etc/docker/daemon.json ] &&
  ! grep -q "$DATA_DIR/docker" /etc/docker/daemon.json && [ -n "$(docker ps -aq 2> /dev/null)" ]; then
  warn "Docker already has containers outside $DATA_DIR/docker: data-root left alone (move it by hand: stop docker, rsync -aHAX /var/lib/docker/ $DATA_DIR/docker/, then set data-root)"
else
  if write_if_changed /etc/docker/daemon.json "$daemon"; then
    say "daemon.json written (local log driver 5 x 20 MB, live-restore$([ -n "$DATA_DEVICE" ] && echo ", data on $DATA_DIR/docker"))"
    DAEMON_CHANGED=yes
  fi
fi
if ! command -v docker > /dev/null || ! docker compose version > /dev/null 2>&1; then
  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  write_if_changed /etc/apt/sources.list.d/docker.sources "Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $ARCH
Signed-By: /etc/apt/keyrings/docker.asc" || true
  apt-get update -q
  apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  say "Docker installed: $(docker --version)"
elif [ "${DAEMON_CHANGED:-no}" = "yes" ]; then
  systemctl restart docker
  say "Docker restarted for the new daemon.json"
else
  say "Docker present: $(docker --version | cut -d, -f1), compose $(docker compose version --short)"
fi
if [ -n "$DATA_DEVICE" ]; then
  # Docker waits for the volume, and does not start without it (rather than
  # starting empty on the boot volume).
  write_if_changed /etc/systemd/system/docker.service.d/10-moa-data.conf "[Unit]
RequiresMountsFor=$DATA_DIR/docker" && systemctl daemon-reload && say "docker waits for $DATA_DIR" || true
fi
systemctl enable --quiet --now docker

# ─── 6. fail2ban, updates, SSH ───────────────────────────────────────────────
step "6. fail2ban, unattended upgrades, SSH"
ignore="127.0.0.1/8 ::1 ${ADMIN_CIDRS[*]}"
if write_if_changed /etc/fail2ban/jail.d/moa.local "[sshd]
enabled = true
backend = systemd
maxretry = 5
findtime = 10m
bantime = 1h
ignoreip = $ignore"; then
  systemctl restart fail2ban
  say "fail2ban: SSH jail on (5 failures in 10 min: banned for an hour)"
else
  say "fail2ban already configured"
fi
systemctl enable --quiet --now fail2ban

write_if_changed /etc/apt/apt.conf.d/20auto-upgrades 'APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";' && say "unattended upgrades on (security updates daily)" || say "unattended upgrades already on"
reboot_lines='Unattended-Upgrade::Automatic-Reboot "false";'
if [ -n "$AUTO_REBOOT" ]; then
  reboot_lines="Unattended-Upgrade::Automatic-Reboot \"true\";
Unattended-Upgrade::Automatic-Reboot-Time \"$AUTO_REBOOT\";"
fi
write_if_changed /etc/apt/apt.conf.d/52moa-unattended-upgrades "// MyOtherApp (ops/server/setup.sh). Security updates only (Ubuntu's default
// origins); Docker's own packages are not among them, so a Docker upgrade is
// always a decision, never a surprise.
Unattended-Upgrade::Remove-Unused-Kernel-Packages \"true\";
Unattended-Upgrade::Remove-Unused-Dependencies \"true\";
$reboot_lines" && say "reboots after updates: ${AUTO_REBOOT:-never automatic (sudo reboot when /var/run/reboot-required exists)}" || true

if write_if_changed /etc/ssh/sshd_config.d/60-moa.conf "# MyOtherApp (ops/server/setup.sh): keys only, no root.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
X11Forwarding no"; then
  sshd -t || die "sshd rejects the new settings (/etc/ssh/sshd_config.d/60-moa.conf)"
  systemctl try-reload-or-restart ssh
  say "SSH: keys only, no root login"
else
  say "SSH already keys only"
fi

# ─── 7. Deploy user ──────────────────────────────────────────────────────────
step "7. The deploy user (moa-deploy: deploy-gate.sh only)"
if ! id moa-deploy > /dev/null 2>&1; then
  useradd --create-home --shell /bin/bash --comment "CI deploys: deploy-gate.sh only" moa-deploy
  passwd -l moa-deploy > /dev/null
  say "user moa-deploy created (no password)"
else
  say "user moa-deploy exists"
fi
install -d -m 700 -o moa-deploy -g moa-deploy /home/moa-deploy/.ssh
if [ -n "$DEPLOY_KEY_FILE" ]; then
  key="$(grep -vE '^\s*(#|$)' "$DEPLOY_KEY_FILE" | head -n 1)"
  [[ "$key" =~ ^(ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa)\ [A-Za-z0-9+/=]+( .*)?$ ]] || die "$DEPLOY_KEY_FILE does not hold one public key"
  if write_if_changed /home/moa-deploy/.ssh/authorized_keys "restrict,command=\"$MOA_DIR/deploy-gate.sh\" $key" 600; then
    chown moa-deploy:moa-deploy /home/moa-deploy/.ssh/authorized_keys
    say "CI key installed, limited to deploy-gate.sh"
  else
    say "CI key already installed"
  fi
else
  say "no --deploy-key-file: CI cannot deploy until one is given"
fi
sudoers="Defaults:moa-deploy !requiretty
moa-deploy ALL=(root) NOPASSWD: $MOA_DIR/deploy.sh"
if write_if_changed /etc/sudoers.d/moa-deploy "$sudoers" 440; then
  visudo -cf /etc/sudoers.d/moa-deploy > /dev/null || { rm -f /etc/sudoers.d/moa-deploy; die "sudoers rule rejected"; }
  say "sudoers: moa-deploy may run $MOA_DIR/deploy.sh as root, nothing else"
fi

# ─── 8. The stack ────────────────────────────────────────────────────────────
step "8. $MOA_DIR"
install -d -m 755 "$MOA_DIR"
install -d -m 755 "$MOA_DIR/caddy"
install -d -m 700 "$MOA_DIR/env" "$MOA_DIR/certs" "$MOA_DIR/secrets" "$MOA_DIR/secrets/walletethings" "$MOA_DIR/state"
if [ ! -f "$MOA_DIR/compose.yml" ] || [ "$UPDATE_STACK" = "yes" ]; then
  [ -f "$HERE/compose.yml" ] || die "run this from a checkout of moa_backend (ops/server/setup.sh), so the stack's files are beside it"
  (cd "$HERE" && find . -type f ! -name '*.moa-new') | while read -r rel; do
    rel="${rel#./}"
    case "$rel" in *.sh | dc) mode=755 ;; *) mode=644 ;; esac
    install -D -m "$mode" "$HERE/$rel" "$MOA_DIR/$rel"
  done
  if [ -d "$HERE/../backup" ]; then
    (cd "$HERE/.." && find backup -type f) | while read -r rel; do
      case "$rel" in *.sh) mode=755 ;; *) mode=644 ;; esac
      install -D -m "$mode" "$HERE/../$rel" "$MOA_DIR/ops/$rel"
    done
  fi
  say "stack files installed from $HERE"
else
  say "stack files already there (--update-stack to replace them; CI does it with deploy.sh stack)"
fi
[ -f "$MOA_DIR/images.env" ] || install -m 644 /dev/null "$MOA_DIR/images.env"
ln -sf "$MOA_DIR/dc" /usr/local/bin/moa-dc
if [ -f "$MOA_DIR/.env" ]; then
  chmod 600 "$MOA_DIR/.env"
  chown root:root "$MOA_DIR/.env"
  say ".env present (600)"
else
  say ".env not there yet: copy the old box's file (docs/MOVE_TO_ORACLE.md, step 3)"
fi

# ─── 9. Backups ──────────────────────────────────────────────────────────────
step "9. Backups (ops/backup)"
if [ -d "$MOA_DIR/ops/backup/systemd" ]; then
  drill_args="--max-age-hours 26"
  [ "$ROLE" = "staging" ] && drill_args="--standalone --max-age-hours 26"
  write_if_changed /etc/default/myotherapp-backup "# ops/server/setup.sh: the backup units' paths on this box.
MOA_PROJECT_DIR=$MOA_DIR
MOA_COMPOSE_FILE=compose.yml
MOA_SERVICE=backend
MOA_DRILL_ARGS=$drill_args" || true
  units_changed=no
  for unit in "$MOA_DIR"/ops/backup/systemd/*; do
    if ! cmp -s "$unit" "/etc/systemd/system/$(basename "$unit")"; then
      install -m 644 "$unit" /etc/systemd/system/
      units_changed=yes
    fi
  done
  [ "$units_changed" = "yes" ] && systemctl daemon-reload
  if [ "$ROLE" = "production" ]; then
    systemctl enable --quiet --now myotherapp-backup.timer
    say "daily backup timer on (02:30 IST): $(systemctl show -p NextElapseUSecRealtime --value myotherapp-backup.timer 2> /dev/null || true)"
  else
    systemctl enable --quiet --now myotherapp-restore-drill.timer
    say "staging: the monthly restore drill timer is on; no daily backup here"
  fi
else
  say "ops/backup is not here yet (backend PR #102): install it with the next 'deploy.sh stack', then run this again"
fi

# ─── 10. Tailscale (optional) ────────────────────────────────────────────────
if [ "$TAILSCALE" = "yes" ]; then
  step "10. Tailscale"
  if ! command -v tailscale > /dev/null; then
    curl -fsSL "https://pkgs.tailscale.com/stable/ubuntu/${UBUNTU_CODENAME:-noble}.noarmor.gpg" -o /usr/share/keyrings/tailscale-archive-keyring.gpg
    curl -fsSL "https://pkgs.tailscale.com/stable/ubuntu/${UBUNTU_CODENAME:-noble}.tailscale-keyring.list" -o /etc/apt/sources.list.d/tailscale.list
    apt-get update -q
    apt-get install -y -q tailscale
  fi
  systemctl enable --quiet --now tailscaled
  if tailscale status > /dev/null 2>&1; then
    say "Tailscale up: $(tailscale ip -4 2> /dev/null | head -n 1)"
  else
    say "Tailscale installed; join it once: sudo tailscale up --advertise-tags=tag:moa-server"
  fi
fi

step "Done"
say "Next: docs/MOVE_TO_ORACLE.md, step 3 (.env, certs, secrets), then: sudo $MOA_DIR/deploy.sh doctor"
