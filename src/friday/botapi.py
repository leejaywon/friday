"""The local Telegram Bot API server (tdlib/telegram-bot-api, deploy/friday-botapi.service).

Telegram's cloud Bot API lets a bot download files up to 20 MB and send up to 50 MB. The same
server run locally with --local lifts both to 2 GB. It connects out to Telegram and listens on
127.0.0.1 only. Friday uses it when TELEGRAM_API_ID (and TELEGRAM_API_HASH) from my.telegram.org
are set; `friday botapi switch` then logs the bot out of the cloud API once.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import httpx
from telegram.ext import ApplicationBuilder

from .config import Config

HOST, PORT = "127.0.0.1", 8081
URL = f"http://{HOST}:{PORT}"
CLOUD_URL = "https://api.telegram.org"
BINARY = "telegram-bot-api"
SERVICE = "friday-botapi.service"
COMMIT_STAMP = Path("/usr/local/share/telegram-bot-api/commit")  # written by deploy/bootstrap.sh

MB = 1024 * 1024
CLOUD_DOWNLOAD_LIMIT = 20 * MB
CLOUD_UPLOAD_LIMIT = 50 * MB
LOCAL_LIMIT = 2000 * MB
# The local server answers getFile / sendDocument once the whole file has moved to or from
# Telegram, which for 2 GB takes minutes.
TRANSFER_TIMEOUT = 3600.0


def download_limit(cfg: Config) -> int:
    return LOCAL_LIMIT if cfg.local_botapi else CLOUD_DOWNLOAD_LIMIT


def upload_limit(cfg: Config) -> int:
    return LOCAL_LIMIT if cfg.local_botapi else CLOUD_UPLOAD_LIMIT


def builder(cfg: Config) -> ApplicationBuilder:
    """An ApplicationBuilder pointed at the local server when TELEGRAM_API_ID is set, else the cloud."""
    b = ApplicationBuilder()
    if cfg.local_botapi:
        b = b.base_url(f"{URL}/bot").base_file_url(f"{URL}/file/bot").local_mode(True)
    return b


def server_command(cfg: Config, binary: str = BINARY) -> list[str]:
    return [binary, "--local", f"--http-ip-address={HOST}", f"--http-port={PORT}",
            f"--dir={cfg.botapi_dir}", f"--temp-dir={cfg.botapi_dir / 'tmp'}"]


def serve(cfg: Config) -> None:
    """Replace this process with the server (what friday-botapi.service runs). It gets the api
    id/hash from .env (parsed like the bot does) and nothing else of Friday's environment."""
    if not (cfg.telegram_api_id and cfg.telegram_api_hash):
        raise SystemExit("TELEGRAM_API_ID and TELEGRAM_API_HASH must both be set in .env.")
    binary = shutil.which(BINARY) or f"/usr/local/bin/{BINARY}"
    (cfg.botapi_dir / "tmp").mkdir(parents=True, exist_ok=True)
    cfg.botapi_dir.chmod(0o700)
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(Path.home()),
           "TELEGRAM_API_ID": cfg.telegram_api_id, "TELEGRAM_API_HASH": cfg.telegram_api_hash}
    os.execve(binary, server_command(cfg, binary), env)


def _call(base: str, token: str, method: str) -> dict:
    """A Bot API call; never raises, and errors never include the URL (it holds the token)."""
    try:
        r = httpx.post(f"{base}/bot{token}/{method}", timeout=30)
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        return {"ok": False, "description": f"{type(e).__name__} (is the server running?)"}


def _local_me(cfg: Config) -> str | None:
    res = _call(URL, cfg.telegram_token, "getMe")
    return f"@{res['result'].get('username')}" if res.get("ok") else None


def status(cfg: Config) -> None:
    def line(ok: bool, text: str) -> None:
        print(("  ok   " if ok else "  --   ") + text)

    line(cfg.local_botapi, "TELEGRAM_API_ID " + ("set: Friday uses the local server" if cfg.local_botapi
                                                 else "not set: Friday uses the cloud Bot API (20 MB files)"))
    line(bool(cfg.telegram_api_hash), "TELEGRAM_API_HASH " + ("set" if cfg.telegram_api_hash else "not set"))
    binary = shutil.which(BINARY) or (f"/usr/local/bin/{BINARY}" if Path(f"/usr/local/bin/{BINARY}").exists() else None)
    version = ""
    if binary:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True)
        version = (out.stdout + out.stderr).strip()
        commit = COMMIT_STAMP.read_text().strip()[:12] if COMMIT_STAMP.exists() else "?"
        version = f"{version} (commit {commit})"
    line(bool(binary), f"{BINARY}: {binary or 'not installed (deploy/bootstrap.sh builds it)'} {version}".rstrip())
    active = subprocess.run(["systemctl", "--user", "is-active", SERVICE], capture_output=True, text=True).stdout.strip()
    line(active == "active", f"{SERVICE}: {active or '?'}")
    if active == "active":
        me = _local_me(cfg)
        line(bool(me), f"local server at {URL}: " + (f"serving {me}" if me else "not answering for this bot"))
    # Deliberately no call to the cloud API: after `switch`, a call there more than 10 minutes
    # later would log the bot back in to the cloud, and then updates may go missing.


def switch(cfg: Config, yes: bool = False) -> None:
    """Log the bot out of the cloud Bot API (logOut), once, so the local server gets its updates."""
    if not cfg.local_botapi:
        raise SystemExit("TELEGRAM_API_ID isn't set. Put TELEGRAM_API_ID and TELEGRAM_API_HASH in .env "
                         "(README: Files up to 2 GB), deploy, then run this.")
    me = _local_me(cfg)
    if not me:
        raise SystemExit(f"The local Bot API server isn't answering at {URL}. Check:\n"
                         f"  systemctl --user status {SERVICE}\n  journalctl --user -u {SERVICE} -n 50")
    print(f"The local server at {URL} serves {me}.\n\n"
          "This calls logOut on Telegram's cloud Bot API (api.telegram.org). After it:\n"
          "  - only the local server serves the bot; Friday already uses it (TELEGRAM_API_ID is set)\n"
          "  - the cloud API refuses this bot for about 10 minutes\n"
          "  - going back to the cloud: remove TELEGRAM_API_ID, call logOut on the local server, deploy\n")
    if not yes and input("Log the bot out of the cloud Bot API now? [y/N] ").strip().lower() not in ("y", "yes"):
        raise SystemExit("Nothing changed.")
    res = _call(CLOUD_URL, cfg.telegram_token, "logOut")
    if not res.get("ok"):
        raise SystemExit(f"logOut failed: {res.get('description', res)}\n"
                         "If it says the bot is already logged out, the switch was done before.")
    print("Logged out of the cloud Bot API. Friday now gets its updates only through the local server.\n"
          f"Check with `friday botapi status`, then send the bot a message. {me} on the local server: "
          + ("ok" if _local_me(cfg) else "NOT answering; see journalctl --user -u " + SERVICE))
