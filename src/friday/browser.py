"""The shared browser: a headless Chromium that deploy/friday-browser.service keeps running.

The chat agent drives it through the Playwright MCP server; Friday's own code connects over the
same DevTools (CDP) port only to type stored logins and take screenshots. The port gives full
control of the logged-in browser, so it listens on 127.0.0.1 only and coding jobs can't reach it.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from .config import Config, env_file
from .vault import Login, domain_allowed

CDP_PORT = 9222  # same as deploy/friday-browser.service
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
PLAYWRIGHT_MCP = "@playwright/mcp@0.0.84"
# deploy.sh installs it here: starts faster than npx, and coding jobs can write to npm's cache
# (~/.npm) but not here.
PLAYWRIGHT_MCP_CLI = Path.home() / ".local/share/friday/playwright-mcp/node_modules/@playwright/mcp/cli.js"
# Tools that would hand the agent what fill_login typed: page JavaScript (reads the password field),
# request bodies (the login POST), and arbitrary Playwright code in the MCP server's process.
BLOCKED_TOOLS = ["browser_evaluate", "browser_run_code_unsafe", "browser_network_request", "browser_network_requests"]

PASSWORD = "input[type=password]:visible"
SUBMIT_BUTTON = "button[type=submit], input[type=submit], button:not([type])"
LEFT_PASSWORD = """([pw, clear]) => {
  let n = 0;
  for (const e of document.querySelectorAll('input[type=password]'))
    if (e.value === pw) { n++; if (clear) e.value = ''; }
  return n;
}"""
# From the password field, the closest visible text/email field before it is the username.
FIND_USERNAME = """pw => {
  const all = [...document.querySelectorAll('input')];
  for (let i = all.indexOf(pw) - 1; i >= 0; i--) {
    const e = all[i], t = (e.getAttribute('type') || 'text').toLowerCase();
    if (['text', 'email', 'tel'].includes(t) && e.offsetParent !== null && !e.disabled) return e;
  }
  return null;
}"""


def private_paths(cfg: Config) -> list[Path]:
    """Hidden from the chat agent's shell and the Playwright MCP server: the vault, the browser
    profile (session cookies), and Friday's other secrets."""
    home = Path.home()
    return [cfg.vault_dir, cfg.browser_profile, env_file(), cfg.google_token,
            home / ".ssh", home / ".codex", home / ".claude", home / ".claude.json"]


def playwright_mcp_command(cfg: Config) -> list[str]:
    """The pinned Playwright MCP server (npx where deploy.sh hasn't installed it), inside bubblewrap
    when available so that its file upload can't read private_paths (its own workspace-only check
    is a guardrail, not a boundary). bwrap needs existing mount points, so only existing paths
    are hidden."""
    run = ["node", str(PLAYWRIGHT_MCP_CLI)] if PLAYWRIGHT_MCP_CLI.exists() else ["npx", "-y", PLAYWRIGHT_MCP]
    cmd = [*run, "--cdp-endpoint", CDP_URL, "--output-dir", str(cfg.files_dir / "browser")]
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return cmd
    hide: list[str] = []
    for p in private_paths(cfg):
        if p.is_dir():
            hide += ["--tmpfs", str(p)]
        elif p.exists():
            hide += ["--ro-bind", "/dev/null", str(p)]
    return [bwrap, "--die-with-parent", "--dev-bind", "/", "/", *hide, "--", *cmd]


def _host(url: str) -> str:
    return urlsplit(url).hostname or url or "?"


def _allowed(url: str, domains: tuple[str, ...]) -> bool:
    # https only: a password must never go over plain http.
    parts = urlsplit(url)
    return parts.scheme == "https" and domain_allowed(parts.hostname or "", domains)


async def _pages(browser) -> list:
    return [p for ctx in browser.contexts for p in ctx.pages]


async def _login_frame(browser, domains: tuple[str, ...]):
    """(frame with a visible password field on an allowed https site, host) — newest tab first.
    Both the tab and the frame must be on an allowed domain."""
    pages = await _pages(browser)
    for page in reversed(pages):
        if not _allowed(page.url, domains):
            continue
        for frame in page.frames:
            if _allowed(frame.url, domains) and await frame.locator(PASSWORD).count():
                return frame, _host(page.url)
    real = [p for p in pages if not p.url.startswith(("about:", "chrome"))]
    return None, _host((real or pages)[-1].url) if pages else "no open page"


async def check_site(domains: tuple[str, ...]) -> tuple[bool, str]:
    """Is a login form for these domains open? Returns (ok, host shown to the user)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        frame, host = await _login_frame(browser, domains)
        return frame is not None, host


async def _settle(page) -> None:
    await asyncio.sleep(2)  # let a navigation start before waiting for it
    try:
        await page.wait_for_load_state("load", timeout=15000)
    except Exception:
        pass


async def _password_left(page, password: str, clear: bool) -> int:
    """How many password fields on the page still hold the password; with clear, empty them."""
    n = 0
    for frame in page.frames:
        try:
            n += await frame.evaluate(LEFT_PASSWORD, [password, clear])
        except Exception:  # a frame that went away mid-navigation
            pass
    return n


async def fill_login(login: Login) -> str:
    """Type the login into the open form and submit it. Returns a short status, never the values.

    It always submits, and empties the password field if the page still holds it: page snapshots
    include field values, so a password left in the page would reach the agent."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        frame, host = await _login_frame(browser, login.domains)  # checked again: the page may have changed
        if frame is None:
            return f"Wrong site: {host}"
        page = frame.page
        pw = frame.locator(PASSWORD).first
        user = (await pw.evaluate_handle(FIND_USERNAME)).as_element()
        if user:
            await user.fill(login.username)
        await pw.fill(login.password)
        await pw.press("Enter")
        await _settle(page)
        if await _password_left(page, login.password, clear=False):  # Enter didn't submit: try the button
            button = frame.locator(f"form:has({PASSWORD}) :is({SUBMIT_BUTTON})").first
            try:
                if await button.count():
                    await button.click(timeout=5000)
                    await _settle(page)
            except Exception:
                pass
        if await _password_left(page, login.password, clear=True):
            return (f"Submitted on {host}, but the login form is still there (wrong password, captcha "
                    "or another step?). The password field was emptied; look at the page.")
        return f"Filled and submitted on {host}"


async def screenshot(path: Path) -> str | None:
    """Screenshot of the newest real tab. Returns its URL, or None if there is nothing to show."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        pages = [pg for pg in await _pages(browser) if not pg.url.startswith(("about:", "chrome"))]
        if not pages:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        await pages[-1].screenshot(path=str(path))
        return pages[-1].url


# --- scheduled runs ------------------------------------------------------------

RUN_TIMEOUT_SECONDS = 1800  # a run may wait for several approvals of up to 5 minutes each
RUN_RULES = {
    "approve": "Mode: approve. Prepare everything (log in, fill in, attach files), then call "
               "request_approval with exactly what you are about to submit, and submit only if it "
               "returns approved.",
    "auto": "Mode: auto. The user approved this run in advance, so you may submit without asking. "
            "If anything is unexpected (another assignment than described, a missing or doubtful file, "
            "an error, a page asking for something the task doesn't cover), stop and report instead.",
}


def run_prompt(run, started: str) -> str:
    """The task for the fresh agent that does a scheduled run (after Friday's instructions)."""
    logins = run["logins"].replace(",", ", ") or "none (fill_login will ask the user)"
    return (
        f"[Scheduled browser run #{run['id']}, started {started}. The user set this up earlier and "
        "isn't watching the chat; your final message is sent to them as the run's report.]\n"
        f"{RUN_RULES[run['mode']]}\n"
        f"Logins fill_login may use without asking: {logins}.\n"
        "Final message: short, in the task's language: what you did and whether it was submitted.\n\n"
        f"Task:\n{run['prompt']}"
    )
