#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 24.04 VPS for Friday. Safe to run again. Run as root:
#   ssh root@SERVER_IP 'bash -s' < deploy/bootstrap.sh
# (deploy/setup.sh runs it for you.)
set -euo pipefail

APP_USER=friday
export DEBIAN_FRONTEND=noninteractive

if [[ $EUID -ne 0 ]]; then echo "Run as root." >&2; exit 1; fi
if [[ ! -s /root/.ssh/authorized_keys ]]; then
  echo "No SSH key in /root/.ssh/authorized_keys. Add your key first (ssh-copy-id), or you'd be locked out." >&2
  exit 1
fi

echo "== packages"
apt-get update -q
apt-get upgrade -yq
apt-get install -yq curl git tmux sqlite3 rsync ufw fail2ban unattended-upgrades bubblewrap socat ca-certificates
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "== Node.js (for the codex CLI)"
if ! command -v node >/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_24.x | bash -
  apt-get install -yq nodejs
fi
npm install -g @openai/codex

echo "== bubblewrap: allow user namespaces (sandbox for coding jobs)"
# Ubuntu's AppArmor blocks unprivileged user namespaces, which bwrap needs. This profile (from
# code.claude.com/docs/en/sandboxing) lifts that for /usr/bin/bwrap only, not machine-wide.
cat > /etc/apparmor.d/bwrap <<'EOF'
abi <abi/4.0>,
include <tunables/global>

profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
  include if exists <local/bwrap>
}
EOF
apparmor_parser -r /etc/apparmor.d/bwrap

echo "== user $APP_USER"
id "$APP_USER" >/dev/null 2>&1 || adduser --disabled-password --gecos "" "$APP_USER"
install -d -m 700 -o "$APP_USER" -g "$APP_USER" /home/$APP_USER/.ssh
install -m 600 -o "$APP_USER" -g "$APP_USER" /root/.ssh/authorized_keys /home/$APP_USER/.ssh/authorized_keys
install -d -o "$APP_USER" -g "$APP_USER" /home/$APP_USER/friday /home/$APP_USER/friday-data /home/$APP_USER/code
loginctl enable-linger "$APP_USER"   # lets its systemd user services run without a login

echo "== uv and Claude Code for $APP_USER"
sudo -u "$APP_USER" -H bash -lc 'command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh'
sudo -u "$APP_USER" -H bash -lc 'test -x ~/.local/bin/claude || curl -fsSL https://claude.ai/install.sh | bash'

echo "== browser: Google Chrome Stable for Friday (H.264/AAC included)"
if [[ $(dpkg --print-architecture) != amd64 ]]; then
  echo "Friday's Google Chrome package requires an amd64 server." >&2
  exit 1
fi
apt-get install -yq fonts-noto-cjk libatomic1 pulseaudio pulseaudio-utils ffmpeg
if ! dpkg-query -W -f='${Status}' google-chrome-stable 2>/dev/null | grep -q 'install ok installed'; then
  chrome_deb=$(mktemp --suffix=.deb)
  curl -fsSL https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o "$chrome_deb"
  chmod 644 "$chrome_deb"
  apt-get install -yq "$chrome_deb"
  rm -f "$chrome_deb"
fi
# Ubuntu ships this profile for Chrome. Reload it, or provide it on minimal images, so Chrome
# can create user namespaces for its sandbox without disabling it or changing global settings.
if [[ ! -f /etc/apparmor.d/chrome ]]; then
cat > /etc/apparmor.d/chrome <<'EOF'
abi <abi/4.0>,
include <tunables/global>

profile chrome /opt/google/chrome/chrome flags=(unconfined) {
  userns,
  include if exists <local/chrome>
}
EOF
fi
apparmor_parser -r /etc/apparmor.d/chrome
# Chrome Stable's policy location differs from Playwright's build. Keep file:// blocked and
# passwords/autofill out of the profile. Root owns the policies; the browser user cannot edit them.
install -d -m 755 /etc/opt/chrome/policies/managed
cat > /etc/opt/chrome/policies/managed/friday.json <<'EOF'
{"URLBlocklist": ["file://*"], "PasswordManagerEnabled": false,
 "AutofillSettings": [{"url_pattern": "*", "blocked_types": ["contact_info", "payments"]}]}
EOF
chmod 644 /etc/opt/chrome/policies/managed/friday.json

# Package caches the job sandbox may write to (src/friday/jobs.py AGENT_CACHES); it ignores
# paths that don't exist yet.
sudo -u "$APP_USER" -H mkdir -p /home/$APP_USER/.cache/uv /home/$APP_USER/.local/share/uv /home/$APP_USER/.npm

echo "== swap (2 GB)"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q /swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== SSH: keys only"
# sshd keeps the first value it reads, and files load in name order, so 00- beats e.g. 50-cloud-init.conf.
rm -f /etc/ssh/sshd_config.d/99-friday.conf
cat > /etc/ssh/sshd_config.d/00-friday.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
systemctl reload ssh

echo "== firewall: SSH only (the bot polls Telegram, so no inbound ports are needed)"
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable

echo
echo "Done. Next: sign in to Codex and Claude, give the server GitHub access, and deploy."
echo "deploy/setup.sh walks through these; README.md has the manual steps."
