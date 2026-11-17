#!/usr/bin/env bash
# Guided setup of your own Friday server, run from your Mac or Linux machine in this repo:
#   deploy/setup.sh
# Each step says what it does, checks whether it's already done, and skips it if so, so it's
# safe to run again (after a failure, or to change settings). It asks before changing anything.
# README.md "Setup" has the same steps done by hand.
#
# Overrides: FRIDAY_HOST (SSH alias for the friday user, default friday), FRIDAY_ROOT_HOST
# (alias for root, default friday-root), FRIDAY_SSH_KEY (default ~/.ssh/friday_ed25519).
#
# Commands quoted for the server use '~', which the server's shell expands.
# shellcheck disable=SC2088
set -euo pipefail

HOST="${FRIDAY_HOST:-friday}"
ROOT_HOST="${FRIDAY_ROOT_HOST:-friday-root}"
KEY="${FRIDAY_SSH_KEY:-$HOME/.ssh/friday_ed25519}"
SSH_CONFIG="$HOME/.ssh/config"
ENV_FILE=".env.server"
# GitHub's published ed25519 host key fingerprint:
# https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints
GITHUB_FP="SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"

ENV_CHANGED=0
STEP=0

if [[ -t 1 ]]; then B=$'\033[1m'; R=$'\033[0m'; else B=""; R=""; fi

step() { STEP=$((STEP + 1)); printf '\n%s== %d. %s%s\n' "$B" "$STEP" "$1" "$R"; }
info() { printf '   %s\n' "$*"; }
ok() { printf '   ok: %s\n' "$*"; }
warn() { printf '   note: %s\n' "$*"; }
die() {
  printf '\nStopped: %s\nFix that and run deploy/setup.sh again; finished steps are skipped.\n' "$*" >&2
  exit 1
}

# confirm "Question?" [Y|N]: the default is what Enter means.
confirm() {
  local answer hint="[Y/n]"
  [[ "${2:-Y}" == N ]] && hint="[y/N]"
  read -r -p "   $1 $hint " answer || answer=""
  answer=$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')
  [[ -z "$answer" ]] && answer=$(printf '%s' "${2:-Y}" | tr '[:upper:]' '[:lower:]')
  [[ "$answer" == y || "$answer" == yes ]]
}

# ask VAR "Prompt" [default]: a visible answer.
ask() {
  local answer
  read -r -p "   $2${3:+ [$3]}: " answer || answer=""
  printf -v "$1" '%s' "${answer:-${3:-}}"
}

# ask_secret VAR "Prompt": typing isn't shown, and the value is never printed.
ask_secret() {
  local answer
  read -r -s -p "   $2 (not shown): " answer || answer=""
  echo
  printf -v "$1" '%s' "$answer"
}

# --- SSH helpers --------------------------------------------------------------

# Works without asking anything (key login, known host)?
can_ssh() { ssh -n -o BatchMode=yes -o ConnectTimeout=15 "$1" true 2>/dev/null; }
on_server() { ssh -n -o BatchMode=yes -o ConnectTimeout=15 "$HOST" "$@"; }
# The HostName an alias points at (ssh prints the alias itself when it isn't configured).
host_of() { ssh -G "$1" 2>/dev/null | awk '$1 == "hostname" { print $2; exit }'; }
has_alias() { [[ "$(host_of "$1")" != "$1" ]]; }

bootstrapped() {
  can_ssh "$HOST" && on_server 'command -v codex >/dev/null && command -v bwrap >/dev/null &&
    test -x ~/.local/bin/uv && test -x ~/.local/bin/claude && test -f /etc/ssh/sshd_config.d/00-friday.conf' 2>/dev/null
}

# --- .env.server helpers ------------------------------------------------------

# The value of KEY (the last uncommented KEY= line, like the dotenv loader), without quotes.
get_env() {
  [[ -f "$ENV_FILE" ]] || return 0
  awk -v k="$1" 'index($0, k "=") == 1 { v = substr($0, length(k) + 2) } END { print v }' "$ENV_FILE" |
    sed -E 's/^"(.*)"$/\1/; s/^'\''(.*)'\''$/\1/'
}

# Set KEY=VALUE: replaces the KEY= line, else uncomments '# KEY=', else appends. The value goes
# through the environment, not the command line, so it doesn't show up in `ps`.
set_env() {
  local tmp
  tmp=$(mktemp "$ENV_FILE.XXXXXX")  # mode 600
  KEY="$1" VAL="$2" awk '
    BEGIN { k = ENVIRON["KEY"]; v = ENVIRON["VAL"] }
    FNR == NR { if (index($0, k "=") == 1) live = FNR; else if (!commented && $0 ~ ("^# *" k "=")) commented = FNR; next }
    FNR == (live ? live : commented) { print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }
  ' "$ENV_FILE" "$ENV_FILE" >"$tmp"
  mv "$tmp" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  ENV_CHANGED=1
}

is_set() { [[ -n "$(get_env "$1")" ]]; }

# Ask for an optional secret; Enter keeps what's there (or leaves it empty).
optional_secret() {
  local key=$1 what=$2 value
  if is_set "$key"; then
    info "$key is set. Paste a new one to replace it, or press Enter to keep it."
  else
    info "$what"
  fi
  ask_secret value "$key (Enter to skip)"
  if [[ -n "$value" ]]; then set_env "$key" "$value"; ok "$key saved"; fi
}

run_push() {
  deploy/push.sh || die "deploy/push.sh failed (see above)."
}

# --- steps ----------------------------------------------------------------------

check_local() {
  step "Check this machine"
  local cmd
  for cmd in ssh ssh-keygen scp git awk sed; do
    command -v "$cmd" >/dev/null || die "'$cmd' isn't installed."
  done
  [[ -f .env.example && -f deploy/bootstrap.sh ]] || die "Run this from the Friday repo (deploy/setup.sh)."
  git check-ignore -q "$ENV_FILE" || die "$ENV_FILE isn't in .gitignore; it would hold your secrets."
  local origin
  origin=$(git remote get-url origin 2>/dev/null) || die "This repo has no 'origin' remote. Push it to your own GitHub repo first."
  origin=${origin%/}
  origin=${origin%.git}
  SLUG=$(printf '%s\n' "$origin" | sed -E -n 's#^.*github[^:/]*[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$#\1/\2#p')
  [[ -n "$SLUG" ]] || die "origin ($origin) isn't a GitHub repo. The server deploys from GitHub."
  ok "repo $SLUG (from git remote origin)"
}

ssh_key() {
  step "SSH key for the server ($KEY)"
  if [[ -f "$KEY" ]]; then
    ok "exists"
  else
    info "Makes a new key pair. A passphrase is optional (Enter for none)."
    if ! confirm "Create it?"; then die "an SSH key is needed."; fi
    mkdir -p "$(dirname "$KEY")" && chmod 700 "$(dirname "$KEY")"
    ssh-keygen -t ed25519 -f "$KEY" -C friday-vps
  fi
  [[ -f "$KEY.pub" ]] || die "$KEY.pub is missing (ssh-keygen -y -f $KEY > $KEY.pub recreates it)."
  info "Public key (paste it into your VPS provider's SSH key field when you order the server):"
  info "$(cat "$KEY.pub")"
}

ssh_config() {
  step "SSH aliases '$HOST' and '$ROOT_HOST' in ~/.ssh/config"
  local ip=""
  has_alias "$HOST" && ip=$(host_of "$HOST")
  [[ -z "$ip" ]] && has_alias "$ROOT_HOST" && ip=$(host_of "$ROOT_HOST")
  if has_alias "$HOST" && has_alias "$ROOT_HOST"; then
    ok "both set up (server $ip)"
    return
  fi
  if [[ -z "$ip" ]]; then
    ask ip "Your server's IP address"
    [[ "$ip" =~ ^[A-Za-z0-9.:-]+$ ]] || die "that doesn't look like an IP address or hostname."
  fi
  mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
  touch "$SSH_CONFIG" && chmod 600 "$SSH_CONFIG"
  local alias user
  for alias in "$HOST" "$ROOT_HOST"; do
    has_alias "$alias" && continue
    user=friday
    [[ "$alias" == "$ROOT_HOST" ]] && user=root
    printf '\nHost %s\n  HostName %s\n  User %s\n  IdentityFile %s\n  IdentitiesOnly yes\n' \
      "$alias" "$ip" "$user" "$KEY" >>"$SSH_CONFIG"
    ok "added Host $alias ($user@$ip)"
  done
}

root_access() {
  step "Root login to the server with your key"
  if bootstrapped; then
    ok "server already prepared; root isn't needed"
    return
  fi
  if can_ssh "$ROOT_HOST"; then
    ok "works"
    return
  fi
  local ip
  ip=$(host_of "$ROOT_HOST")
  if ! ssh-keygen -F "$ip" >/dev/null 2>&1; then
    info "First connection: ssh shows the server's key fingerprint. If your provider's console"
    info "shows it, compare them; then answer yes."
    ssh -o PreferredAuthentications=publickey -o ConnectTimeout=15 "$ROOT_HOST" true || true
  fi
  if ! can_ssh "$ROOT_HOST"; then
    info "Your key isn't on the server's root account yet. ssh-copy-id adds it, asking once for"
    info "the root password from your provider."
    if confirm "Run ssh-copy-id now?"; then
      ssh-copy-id -i "$KEY.pub" "$ROOT_HOST" || true
    fi
  fi
  can_ssh "$ROOT_HOST" || die "can't log in as root@$ip with $KEY."
  ok "works"
}

bootstrap() {
  step "Prepare the server (deploy/bootstrap.sh as root)"
  if bootstrapped; then
    ok "done (the friday user, Codex, Claude Code, uv and the sandbox are installed)"
    return
  fi
  info "Installs packages, Node + Codex CLI, Claude Code, uv and Chromium, creates the 'friday'"
  info "user, turns off SSH password login and enables a firewall that allows only SSH."
  info "Takes a few minutes."
  if ! confirm "Run it on $(host_of "$ROOT_HOST")?"; then die "the server needs bootstrap.sh."; fi
  ssh "$ROOT_HOST" 'bash -s' <deploy/bootstrap.sh
  bootstrapped || die "bootstrap.sh finished, but the server doesn't look ready (can't log in as '$HOST'?)."
  ok "done"
}

env_file() {
  step "Bot settings ($ENV_FILE, uploaded to the server as ~/friday/.env)"
  local created=0
  if [[ ! -f "$ENV_FILE" ]]; then
    (umask 077 && cp .env.example "$ENV_FILE")
    created=1
    ok "created from .env.example"
  fi
  chmod 600 "$ENV_FILE"

  local value
  if is_set TELEGRAM_BOT_TOKEN; then
    ok "TELEGRAM_BOT_TOKEN is set"
  else
    info "In Telegram, message @BotFather, send /newbot, pick a name, and copy the token."
    while :; do
      ask_secret value "Bot token"
      [[ "$value" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]] && break
      info "That doesn't look like a bot token (digits:letters). Try again."
    done
    set_env TELEGRAM_BOT_TOKEN "$value"
    ok "TELEGRAM_BOT_TOKEN saved"
  fi

  if is_set TELEGRAM_ALLOWED_USER_IDS; then
    ok "TELEGRAM_ALLOWED_USER_IDS is set"
  else
    info "Your Telegram user id: the bot tells you after the first deploy (a later step)."
    ask value "Enter it now if you know it, or press Enter"
    if [[ -n "$value" ]]; then
      [[ "$value" =~ ^[0-9]+$ ]] || die "a Telegram user id is a number."
      set_env TELEGRAM_ALLOWED_USER_IDS "$value"
    fi
  fi

  if [[ $created == 0 ]] && ! confirm "Review the timezone and optional keys?" N; then
    return
  fi

  while :; do
    ask value "Your timezone" "$(get_env FRIDAY_TZ)"
    [[ -f "/usr/share/zoneinfo/$value" ]] && break
    info "Unknown timezone. Use a name like Asia/Seoul, Europe/Paris or America/New_York."
  done
  [[ "$value" != "$(get_env FRIDAY_TZ)" ]] && set_env FRIDAY_TZ "$value"
  ok "timezone $value"

  info "Optional keys. Press Enter to skip any of them; you can add them later the same way."
  optional_secret GITHUB_CODE_TOKEN "Coding jobs (/code) push branches and open pull requests with a fine-grained GitHub token (Contents and Pull requests: Read and write, only the repos Friday may change). See 'Coding jobs' in README.md."
  optional_secret GITHUB_TOKEN "Read-only GitHub access for the chat (Contents, Issues, Pull requests: Read). See 'GitHub' in README.md."

  local provider
  provider=$(get_env TRANSCRIBE_PROVIDER)
  info "Voice messages: Friday transcribes them with Groq (https://console.groq.com/keys) or OpenAI."
  while :; do
    ask provider "Provider, groq or openai" "${provider:-groq}"
    [[ "$provider" == groq || "$provider" == openai ]] && break
  done
  optional_secret TRANSCRIBE_API_KEY "API key for $provider (empty = voice messages are only saved)."
  if is_set TRANSCRIBE_API_KEY && [[ "$provider" != "$(get_env TRANSCRIBE_PROVIDER)" ]]; then
    set_env TRANSCRIBE_PROVIDER "$provider"
  fi
}

github_access() {
  step "Server access to GitHub ($SLUG, deploy key with write access)"
  command -v gh >/dev/null || die "install GitHub CLI (https://cli.github.com), run 'gh auth login', then run this again."
  gh auth status >/dev/null 2>&1 || die "GitHub CLI isn't signed in. Run 'gh auth login'."
  local perm
  perm=$(gh repo view "$SLUG" --json viewerPermission -q .viewerPermission 2>/dev/null) || perm=""
  [[ "$perm" == ADMIN ]] || die "you need admin rights on $SLUG to add a deploy key. Use your own fork or copy as origin."

  # A key only for this repo, and an SSH alias that uses it.
  ssh "$HOST" 'bash -s' <<'EOF'
set -euo pipefail
if [[ ! -f ~/.ssh/github_friday ]]; then
  ssh-keygen -q -t ed25519 -N "" -C "friday-vps deploy key" -f ~/.ssh/github_friday
  echo "   ok: made ~/.ssh/github_friday on the server"
fi
touch ~/.ssh/config && chmod 600 ~/.ssh/config
if ! grep -q '^Host github-friday$' ~/.ssh/config; then
  printf '\nHost github-friday\n  HostName github.com\n  User git\n  IdentityFile ~/.ssh/github_friday\n  IdentitiesOnly yes\n' >>~/.ssh/config
  echo "   ok: added Host github-friday to the server's ~/.ssh/config"
fi
EOF

  local pub keys
  pub=$(on_server 'cat ~/.ssh/github_friday.pub' | awk '{ print $1 " " $2 }')
  keys=$(gh api "repos/$SLUG/keys" --jq '.[] | "\(.key) \(.read_only)"') || die "couldn't list $SLUG's deploy keys."
  if grep -qxF "$pub false" <<<"$keys"; then
    ok "deploy key registered on $SLUG"
  elif grep -qF "$pub" <<<"$keys"; then
    die "the server's key is on $SLUG read-only. Delete it under Settings > Deploy keys and run this again."
  else
    if ! confirm "Add the server's key to $SLUG as a deploy key with write access?"; then
      die "the server needs the deploy key to fetch the code."
    fi
    local tmp
    tmp=$(mktemp)
    printf '%s friday-vps\n' "$pub" >"$tmp"
    gh repo deploy-key add "$tmp" --repo "$SLUG" --allow-write --title "friday-vps"
    rm -f "$tmp"
  fi

  # Trust github.com only if its key matches GitHub's published fingerprint.
  ssh "$HOST" 'bash -s' -- "$GITHUB_FP" <<'EOF'
set -euo pipefail
expected=$1
touch ~/.ssh/known_hosts
if ssh-keygen -F github.com >/dev/null; then
  echo "   ok: github.com already in the server's known_hosts"
  exit 0
fi
tmp=$(mktemp)
ssh-keyscan -t ed25519 github.com 2>/dev/null >"$tmp" || true
got=$(ssh-keygen -lf "$tmp" 2>/dev/null | awk '{ print $2 }')
if [[ "$got" != "$expected" ]]; then
  rm -f "$tmp"
  echo "github.com's key fingerprint is '$got', expected $expected. Not trusting it." >&2
  exit 1
fi
cat "$tmp" >>~/.ssh/known_hosts
rm -f "$tmp"
echo "   ok: trusted github.com ($got)"
EOF

  # ~/code/friday is where code gets changed; ~/friday is what runs (each deploy resets it).
  ssh "$HOST" 'bash -s' -- "$SLUG" <<'EOF'
set -euo pipefail
url="git@github-friday:$1.git"
git ls-remote -q "$url" HEAD >/dev/null
for dir in ~/code/friday ~/friday; do
  if [[ -d "$dir/.git" ]]; then
    echo "   ok: $dir already cloned"
  elif [[ -d "$dir" && -n "$(ls -A "$dir")" ]]; then
    echo "$dir exists and isn't empty or a git checkout. Move it aside and run setup again." >&2
    exit 1
  else
    git clone -q "$url" "$dir"
    echo "   ok: cloned $1 to $dir"
  fi
done
EOF
}

codex_login() {
  step "Sign in to Codex on the server (your ChatGPT plan)"
  if on_server 'codex login status' >/dev/null 2>&1; then
    ok "signed in"
    return
  fi
  info "Shows a code and a link: open the link on any device and enter the code."
  if confirm "Sign in now?"; then
    ssh -t "$HOST" 'codex login --device-auth'
  else
    warn "skipped; Friday can't answer until Codex is signed in."
  fi
}

claude_login() {
  step "Sign in to Claude Code on the server (coding jobs)"
  if on_server 'test -s ~/.claude/.credentials.json' 2>/dev/null; then
    ok "signed in"
    return
  fi
  info "Opens Claude on the server. Run /login and follow it, trust the folder when asked, then /exit."
  if confirm "Open it now?"; then
    ssh -t "$HOST" 'cd ~/code && ~/.local/bin/claude' || true
  else
    warn "skipped; coding jobs with Claude need it (CODE_ENGINE=codex works without)."
  fi
}

google_calendar() {
  step "Google Calendar (optional)"
  if [[ -f data/google_token.json ]]; then
    ok "connected (data/google_token.json; deploy/push.sh uploads it)"
  else
    info "Not connected. To add it later, follow 'Connect Google Calendar' in README.md"
    info "(uv run friday google-auth on this machine), then run deploy/push.sh."
  fi
}

first_deploy() {
  step "Deploy (deploy/push.sh: uploads $ENV_FILE, the server pulls GitHub's main and restarts)"
  if on_server 'systemctl --user is-active --quiet friday' 2>/dev/null; then
    if [[ $ENV_CHANGED == 1 ]]; then
      if confirm "Friday is running. Deploy again to upload the changed settings?"; then run_push; fi
    else
      ok "Friday is running"
    fi
    return
  fi
  if confirm "Deploy now?"; then
    run_push
    ENV_CHANGED=0
  else
    die "not deployed yet."
  fi
}

telegram_user() {
  step "Your Telegram user id (only you can talk to the bot)"
  if is_set TELEGRAM_ALLOWED_USER_IDS; then
    ok "TELEGRAM_ALLOWED_USER_IDS is set"
    return
  fi
  info "Open your bot in Telegram and send it any message. It replies with your user id."
  local uid
  while :; do
    ask uid "Your user id"
    [[ "$uid" =~ ^[0-9]+$ ]] && break
    info "It's a number, like 123456789."
  done
  set_env TELEGRAM_ALLOWED_USER_IDS "$uid"
  ok "saved; deploying again so the bot answers only you"
  run_push
  ENV_CHANGED=0
}

main() {
  cd "$(dirname "$0")/.."
  [[ -t 0 ]] || die "run this in a terminal; it asks questions."
  echo "${B}Friday setup${R}: sets up your server step by step. Steps already done are skipped."
  check_local
  ssh_key
  ssh_config
  root_access
  bootstrap
  env_file
  github_access
  codex_login
  claude_login
  google_calendar
  first_deploy
  telegram_user
  echo
  echo "${B}Done.${R} Friday should answer you in Telegram now. Health check:"
  echo "   ssh $HOST 'cd ~/friday && .venv/bin/friday doctor'"
  echo "README.md 'Using it' explains the rest (coding jobs, browser logins, voice)."
}

main "$@"
