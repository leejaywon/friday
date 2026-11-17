#!/usr/bin/env bash
# Runs on the server: puts ~/friday at GitHub's main and restarts Friday.
# Only commits pushed to GitHub go live; ~/friday is never edited by hand.
#
#   bash ~/friday/deploy/deploy.sh      from Claude on the VPS, after `git push`
#   deploy/push.sh                       from the Mac (uploads secrets, then runs this)
#
# Everything sits in main() so bash has read the whole file before git rewrites it.
set -euo pipefail

main() {
  export PATH="$HOME/.local/bin:$PATH"
  cd ~/friday

  if [[ -z "${FRIDAY_DEPLOY_FRESH:-}" ]]; then
    git fetch -q origin main
    local before
    before=$(git rev-parse --short HEAD)
    git reset -q --hard origin/main
    echo "== code $before -> $(git rev-parse --short HEAD): $(git log -1 --format=%s)"
    # Continue with the version of this script that was just checked out.
    FRIDAY_DEPLOY_FRESH=1 exec bash deploy/deploy.sh
  fi

  echo "== install and restart"
  uv sync --frozen --no-dev -q
  # Starter MCP config, only if there isn't one yet (the server copy is the one you edit).
  [[ -f ~/friday-data/mcp.toml ]] || cp deploy/mcp.example.toml ~/friday-data/mcp.toml
  # The shared browser: Chromium for the pinned playwright package (a no-op when installed; its
  # system packages come from bootstrap.sh) and the Playwright MCP server pinned in browser.py.
  .venv/bin/playwright install chromium
  npm install -s --no-fund --no-audit --prefix ~/.local/share/friday/playwright-mcp \
    "$(grep -o '@playwright/mcp@[0-9.]*' src/friday/browser.py)"
  mkdir -p ~/.config/systemd/user
  cp deploy/friday.service deploy/claude-rc.service deploy/friday-browser.service ~/.config/systemd/user/
  systemctl --user daemon-reload
  systemctl --user enable -q friday.service
  # Started if it isn't running, not restarted: that would interrupt a browser run in progress.
  systemctl --user enable -q --now friday-browser.service
  systemctl --user restart friday.service
  sleep 3
  systemctl --user --no-pager --lines=15 status friday.service || true
}

main "$@"; exit
