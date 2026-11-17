#!/usr/bin/env bash
# Deploy from the Mac: uploads secrets, then the server pulls GitHub's main and restarts.
#   deploy/push.sh                      (FRIDAY_HOST defaults to the `friday` SSH alias)
# Commit and `git push` first: the server deploys what is on GitHub, not this folder.
set -euo pipefail

HOST="${FRIDAY_HOST:-friday}"
cd "$(dirname "$0")/.."

git fetch -q origin main
if [[ -n "$(git rev-list origin/main..HEAD)" ]]; then
  echo "You have commits that aren't on GitHub's main yet. Run 'git push' first." >&2
  exit 1
fi
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "Note: uncommitted changes in this folder are not deployed."
fi
if ! ssh "$HOST" 'test -d ~/friday/.git'; then
  echo "The server's ~/friday isn't a git checkout yet. See 'Server access to GitHub' in README.md." >&2
  exit 1
fi

# Secrets go with scp (macOS's bundled rsync has no --chmod), then get locked down.
if [[ -f .env.server ]]; then
  echo "== settings (.env.server -> ~/friday/.env)"
  scp -q .env.server "$HOST:friday/.env"
fi
for f in google_token.json google_client_secret.json; do
  if [[ -f "data/$f" ]]; then
    echo "== $f"
    scp -q "data/$f" "$HOST:friday-data/$f"
  fi
done
ssh "$HOST" 'chmod 600 ~/friday/.env ~/friday-data/*.json 2>/dev/null || true'

ssh "$HOST" 'bash ~/friday/deploy/deploy.sh'
