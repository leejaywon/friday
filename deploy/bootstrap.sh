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

echo "== browser: Playwright's Chromium for Friday (deploy/friday-browser.service)"
PLAYWRIGHT=1.65.0  # keep in step with playwright in pyproject.toml
apt-get install -yq fonts-noto-cjk libatomic1   # CJK text in screenshots; Node bundled with the playwright package
npx -y "playwright@$PLAYWRIGHT" install-deps chromium
sudo -u "$APP_USER" -H bash -lc "npx -y playwright@$PLAYWRIGHT install chromium"
# Like bwrap above: let this Chromium create user namespaces for its own sandbox, which matters
# since it opens arbitrary sites. Only this binary, not machine-wide.
cat > /etc/apparmor.d/friday-chrome <<EOF
abi <abi/4.0>,
include <tunables/global>

profile friday-chrome /home/$APP_USER/.cache/ms-playwright/chromium-*/chrome-linux*/chrome flags=(unconfined) {
  userns,
  include if exists <local/friday-chrome>
}
EOF
apparmor_parser -r /etc/apparmor.d/friday-chrome
# Chrome policy (this build reads chrome_for_testing): no file:// pages, so the agent driving the
# browser can't open files on the server, and no saved passwords or autofill in the profile.
install -d /etc/opt/chrome_for_testing/policies/managed
cat > /etc/opt/chrome_for_testing/policies/managed/friday.json <<'EOF'
{"URLBlocklist": ["file://*"], "PasswordManagerEnabled": false,
 "AutofillAddressEnabled": false, "AutofillCreditCardEnabled": false}
EOF

# Package caches the job sandbox may write to (src/friday/jobs.py AGENT_CACHES); it ignores
# paths that don't exist yet.
sudo -u "$APP_USER" -H mkdir -p /home/$APP_USER/.cache/uv /home/$APP_USER/.local/share/uv /home/$APP_USER/.npm

echo "== telegram-bot-api: local Bot API server, for files up to 2 GB (deploy/friday-botapi.service)"
# Built from source at a pinned commit (the project has no release tags); skipped when that commit
# is already installed. Only used once TELEGRAM_API_ID is set in .env (README: "Files up to 2 GB").
BOTAPI_COMMIT=7232881b39dfcd048a0d569f1983c5184249ee05   # "Update version to 10.4." (2026-11-09)
BOTAPI_STAMP=/usr/local/share/telegram-bot-api/commit
if [[ -x /usr/local/bin/telegram-bot-api && "$(cat "$BOTAPI_STAMP" 2>/dev/null)" == "$BOTAPI_COMMIT" ]]; then
  echo "already built at $BOTAPI_COMMIT"
else
  apt-get install -yq make git zlib1g-dev libssl-dev gperf cmake g++
  BOTAPI_SRC=$(mktemp -d)
  git clone -q --recursive https://github.com/tdlib/telegram-bot-api.git "$BOTAPI_SRC"
  git -C "$BOTAPI_SRC" checkout -q "$BOTAPI_COMMIT"
  git -C "$BOTAPI_SRC" submodule update -q --init --recursive
  cmake -S "$BOTAPI_SRC" -B "$BOTAPI_SRC/build" -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local
  # TDLib's largest files take a few GB of RAM each to compile: two at a time, capped and niced so
  # a running Friday isn't the one the OOM killer picks. Takes 20-40 minutes.
  systemd-run --scope --quiet --nice=15 -p MemoryMax=5G -p MemorySwapMax=2G \
    cmake --build "$BOTAPI_SRC/build" --target install -j 2
  install -d "$(dirname "$BOTAPI_STAMP")"
  echo "$BOTAPI_COMMIT" > "$BOTAPI_STAMP"
  rm -rf "$BOTAPI_SRC"
fi

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
