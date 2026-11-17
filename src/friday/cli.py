"""Command line: `friday bot | ask | google-auth | doctor | mcp | vault`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(prog="friday", description="Personal Telegram agent.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("bot", help="run the Telegram bot (what the systemd service runs)")
    ask = sub.add_parser("ask", help="send one message to the agent from the terminal")
    ask.add_argument("message")
    auth = sub.add_parser("google-auth", help="connect Google Calendar (run on a machine with a browser)")
    auth.add_argument("--client-secret", type=Path, help="OAuth client JSON from Google Cloud Console")
    sub.add_parser("doctor", help="check the configuration")
    sub.add_parser("mcp", help="run the MCP tool server on stdio (Codex starts this itself)")
    vault = sub.add_parser("vault", help="website logins for the browser (Telegram: /login, /logins)")
    vsub = vault.add_subparsers(dest="vault_cmd", required=True)
    vadd = vsub.add_parser("add", help="add or replace a login; asks for the username and password")
    vadd.add_argument("name", help="short name, e.g. school")
    vadd.add_argument("--domain", action="append", required=True,
                      help="site it may be typed into, e.g. lms.example.edu (subdomains included; repeatable)")
    vsub.add_parser("list", help="names, domains and where they're trusted (never passwords)")
    vrm = vsub.add_parser("remove", help="delete a login")
    vrm.add_argument("name")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.cmd == "mcp":
        from .mcp_server import run

        run()
        return

    from .config import ensure_dirs, load_config

    cfg = load_config()
    ensure_dirs(cfg)

    if args.cmd == "vault":
        vault_cmd(cfg, args)
    elif args.cmd == "bot":
        from .bot import run_bot

        run_bot(cfg)
    elif args.cmd == "ask":
        from .agent import instructions, run_codex

        result = asyncio.run(run_codex(cfg, instructions(cfg) + "\n---\n" + args.message))
        print(result.text or f"ERROR: {result.errors}")
    elif args.cmd == "google-auth":
        from . import gcal

        secret = args.client_secret or cfg.google_client_secret
        if not secret.exists():
            raise SystemExit(f"OAuth client file not found: {secret}")
        import json

        if "installed" not in json.loads(secret.read_text()):
            raise SystemExit(
                "This OAuth client is not a 'Desktop app' client. In Google Cloud Console → "
                "Google Auth Platform → Clients, create a client with Application type 'Desktop app', "
                f"download its JSON and save it as {secret}."
            )
        gcal.authorize(secret, cfg.google_token)
        print(f"Saved {cfg.google_token}")
    elif args.cmd == "doctor":
        doctor(cfg)


def vault_cmd(cfg, args) -> None:
    from getpass import getpass

    from .vault import Vault, check

    v = Vault(cfg.vault_dir)
    if args.vault_cmd == "add":
        try:
            check(args.name, args.domain)
            username = input("Username: ").strip()
            password = getpass("Password (not shown): ")
            v.add(args.name, username, password, args.domain)
        except ValueError as e:
            raise SystemExit(f"Not saved: {e}")
        print(f"Saved '{args.name}' for {', '.join(args.domain)}. Friday asks you on Telegram before its first use on each site.")
    elif args.vault_cmd == "list":
        for name, domains, trusted in v.entries():
            trust = f"trusted on {', '.join(trusted)}" if trusted else "asks before use"
            print(f"{name}: {', '.join(domains)} ({trust})")
    elif args.vault_cmd == "remove":
        print("Removed." if v.remove(args.name) else f"No login named '{args.name}'.")


def doctor(cfg) -> None:
    from . import gcal

    def line(ok: bool, text: str) -> None:
        print(("  ok   " if ok else "  FIX  ") + text)

    print(f"data dir: {cfg.data_dir}")
    line(bool(cfg.telegram_token), "TELEGRAM_BOT_TOKEN set")
    line(bool(cfg.allowed_user_ids), f"TELEGRAM_ALLOWED_USER_IDS = {list(cfg.allowed_user_ids)}")
    codex = shutil.which(cfg.codex_bin)
    line(bool(codex), f"codex found: {codex}")
    if codex:
        out = subprocess.run([codex, "login", "status"], capture_output=True, text=True)
        status = (out.stdout + out.stderr).strip().splitlines()
        line(out.returncode == 0, f"codex login: {status[-1] if status else '?'}")
    line(gcal.is_connected(cfg.google_token), f"Google Calendar token: {cfg.google_token}")
    print(f"  --   timezone {cfg.default_tz}, meeting heads-up {cfg.meeting_lead_min} min, "
          f"morning brief {cfg.morning_brief_at or 'off'}")


if __name__ == "__main__":
    main()
