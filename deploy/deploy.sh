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
  cp deploy/friday.service deploy/friday-browser.service \
    deploy/friday-mcp.service deploy/friday-playwright-mcp.service ~/.config/systemd/user/
  systemctl --user daemon-reload
  systemctl --user enable -q friday.service
  # Started if it isn't running, not restarted: that would interrupt a browser run in progress.
  systemctl --user enable -q --now friday-browser.service
  # The local Bot API server was removed. Where an older version set it up, log the bot out of it
  # (so Telegram's cloud API takes the bot back) and stop it.
  if [[ -f ~/.config/systemd/user/friday-botapi.service ]]; then
    .venv/bin/python -c 'import httpx; from friday.config import load_config as c
httpx.post(f"http://127.0.0.1:8081/bot{c().telegram_token}/logOut", timeout=30)' 2>/dev/null \
      && echo "logged the bot out of the local Bot API server" || true
    systemctl --user disable -q --now friday-botapi.service 2>/dev/null || true
    rm -f ~/.config/systemd/user/friday-botapi.service
  fi
  # Friday's MCP tools and the Playwright MCP server as local services, so a chat turn doesn't
  # start them (src/friday/mcp_http.py). Restarted for new code; a browser run in progress is
  # interrupted by Friday's restart anyway.
  systemctl --user enable -q friday-mcp.service
  systemctl --user restart friday-mcp.service
  if .venv/bin/python -c 'from friday.config import load_config as c; raise SystemExit(not c().browser_enabled)'; then
    systemctl --user enable -q friday-playwright-mcp.service
    systemctl --user restart friday-playwright-mcp.service
  else
    systemctl --user disable -q --now friday-playwright-mcp.service 2>/dev/null || true
  fi
  for _ in $(seq 30); do (exec 3<>/dev/tcp/127.0.0.1/8765) 2>/dev/null && break; sleep 0.5; done
  systemctl --user restart friday.service
  sleep 3
  systemctl --user --no-pager --lines=15 status friday.service || true
}

main "$@"; exit
