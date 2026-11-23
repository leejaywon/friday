"""Keeps the shared browser usable: restarts Chrome when it is stuck, and cleans up finished tabs.

Stuck: Chrome can stay alive and answer DevTools HTTP while Playwright can no longer connect, which
systemd can't see. So the check is a real connect_over_cdp, in a child process that is killed at a
deadline: no step of it, cleanup included, can hang the bot. Doctor decides when to restart.

Tabs: Playwright MCP starts every session on the browser's first tab and keeps a popup out of the
session until it is selected, so tasks would work in each other's tabs. browser_proxy gives each
task (a chat lane, a scheduled run, a watch check) its own tab and records it here as a lease in
the kv table. A finished one-shot task releases it; a chat lane's lasts until it has been idle for
a while, since "now open the next one" needs the page. sweep closes what no task holds any more.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field

import httpx

from .browser import CDP_URL

log = logging.getLogger(__name__)

UNIT = "friday-browser.service"
OWNER_ENV = "FRIDAY_TAB_OWNER"  # which task a browser_proxy works for (agent.codex_overrides)
PROBE_SECONDS = 25  # the whole check, child process included
CONNECT_MS = 12_000
RESTART_SECONDS = 120  # systemd gives a stuck Chrome 90 seconds to stop before killing it
FAILS_TO_RESTART = 2  # failed checks in a row
RESTART_COOLDOWN = 15 * 60  # also across Friday's own restarts
CHECK_EVERY = 5 * 60  # while idle
CHECK_FRESH = 60  # a check this recent is good enough before browser work
RESTARTED_KEY = "browser_restarted_at"
LEASE = "browser_tabs:"
RELEASED = LEASE + "done:"
PLAYING = "() => [...document.querySelectorAll('video, audio')].some(m => !m.paused && !m.ended)"


def owner(run_id: int | None = None, watch_id: int | None = None, chat: bool | str = False) -> str:
    """The lease name of a task; "" for one outside the bot."""
    if run_id:
        return f"run:{run_id}"
    if watch_id:
        return f"watch:{watch_id}"
    return f"chat:{chat if isinstance(chat, str) else 1}" if chat else ""


# --- the check -----------------------------------------------------------------------------------


@dataclass
class Probe:
    ok: bool
    playing: set[str] = field(default_factory=set)  # of the tabs asked about; unknown counts as playing
    why: str = ""
    unknown: bool = False  # the check itself failed: no reason to restart Chrome


async def _page_playing(page) -> bool:
    try:
        async with asyncio.timeout(4):
            for frame in page.frames:
                with suppress(Exception):  # a frame that went away
                    if await frame.evaluate(PLAYING):
                        return True
    except TimeoutError:
        return True
    return False


async def _look(tabs: list[str]) -> None:
    """The child process: connect as Playwright does, read-only, print the result, and take the
    Playwright driver down with it instead of waiting for a clean disconnect. no_defaults: leave
    the pages' focus and viewport emulation alone."""
    from playwright.async_api import async_playwright

    try:
        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.connect_over_cdp(CDP_URL, timeout=CONNECT_MS, no_defaults=True)
        except Exception as e:
            print(json.dumps({"ok": False, "why": f"Playwright can't connect ({type(e).__name__})"}), flush=True)
            return
        playing = []
        for ctx in browser.contexts if tabs else []:
            for page in ctx.pages:
                try:
                    async with asyncio.timeout(3):
                        info = await (await ctx.new_cdp_session(page)).send("Target.getTargetInfo")
                except Exception:  # can't tell which tab this is: keep them all this time
                    playing = list(tabs)
                    break
                tid = info["targetInfo"]["targetId"]
                if tid in tabs and await _page_playing(page):
                    playing.append(tid)
        print(json.dumps({"ok": True, "playing": playing}), flush=True)
    finally:
        if os.getpgrp() == os.getpid():
            os.killpg(0, signal.SIGKILL)


async def probe(tabs: list[str] | tuple[str, ...] = ()) -> Probe:
    """Can Playwright really connect? With tabs, also which of them are playing media. Never
    navigates or brings anything to the front, and always returns within PROBE_SECONDS."""
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "friday.browser_care", "probe", *tabs,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        return Probe(False, why=type(e).__name__)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), PROBE_SECONDS)
    except (TimeoutError, asyncio.CancelledError) as e:
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        if isinstance(e, asyncio.CancelledError):
            raise
        return Probe(False, why=f"no connection in {PROBE_SECONDS}s")
    try:
        data = json.loads(out)
    except ValueError:  # the check itself broke (it never got to connect): says nothing about Chrome
        return Probe(False, why="the check didn't run", unknown=True)
    return Probe(bool(data.get("ok")), set(data.get("playing") or ()), str(data.get("why") or ""))


# --- the restart ---------------------------------------------------------------------------------


def _http() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=CDP_URL, timeout=3, trust_env=False)


async def open_tabs() -> dict[str, str] | None:
    """Open tabs (target id -> URL) over DevTools HTTP; None if it doesn't answer."""
    try:
        async with _http() as http:
            items = (await http.get("/json/list")).json()
    except (httpx.HTTPError, ValueError):
        return None
    return {t["id"]: t.get("url", "") for t in items if t.get("type") == "page"}


async def close_tabs(ids: set[str], live: dict[str, str]) -> None:
    """Close tabs, leaving at least one page: headless Chrome with none is of no use to anyone."""
    async with _http() as http:
        if set(live) <= ids:
            await http.put("/json/new?about:blank")
        for tid in ids:
            with suppress(httpx.HTTPError):
                await http.get(f"/json/close/{tid}")


async def close_restored(seconds: float = 25) -> None:
    """After a restart: back to one blank tab. Chrome brings its last tabs back, for a moment
    after it starts too. --restore-last-session stays on because it is what keeps session cookies
    (the LMS logins) across restarts; closing the tabs over DevTools keeps the cookies, while
    moving Chrome's Session files aside did not even stop the restore."""
    deadline, calm = time.monotonic() + seconds, 0
    while calm < 3 and time.monotonic() < deadline:
        await asyncio.sleep(1)
        live = await open_tabs()
        if live is None:
            calm = 0
            continue
        keep = next((t for t, url in live.items() if url == "about:blank"), None)
        extra = {t for t in live if t != keep}
        calm = 0 if extra or keep is None else calm + 1
        with suppress(httpx.HTTPError):
            await close_tabs(extra, live)


async def restart_chrome() -> str:
    """Restart the unit and verify with Playwright. "" when it works again, else why not."""
    try:
        proc = await asyncio.create_subprocess_exec("systemctl", "--user", "restart", UNIT,
                                                    stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.PIPE)
    except OSError as e:
        return f"can't run systemctl ({type(e).__name__})"
    try:
        _, err = await asyncio.wait_for(proc.communicate(), RESTART_SECONDS)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return f"systemctl didn't finish in {RESTART_SECONDS}s"
    if proc.returncode:
        return (err or b"").decode(errors="replace").strip()[-200:] or f"systemctl exit {proc.returncode}"
    await close_restored()
    result = await probe()
    return "" if result.ok else result.why or "Playwright still can't connect"


class Doctor:
    """When to restart Chrome (how is `repair`): after FAILS_TO_RESTART failed checks in a row, only
    while no browser work runs, at most once per RESTART_COOLDOWN, with one notice per incident.
    Work running on a stuck browser is left alone: its tool calls fail and it reports that itself,
    and the restart comes when it has ended."""

    def __init__(self, repair, busy, tell, store, check=probe, clock=time.time):
        self.repair, self.busy, self.tell, self.store, self._check, self.clock = repair, busy, tell, store, check, clock
        self.fails = 0
        self.checked = 0.0  # when Playwright last connected
        self.told = self.waited = self.repairing = False
        self._one = asyncio.Lock()

    def fresh(self) -> bool:
        return not self.fails and self.clock() - self.checked < CHECK_FRESH

    def due(self) -> bool:
        return bool(self.fails) or self.clock() - self.checked >= CHECK_EVERY

    async def _say(self, text: str) -> None:
        try:
            await self.tell(text)
        except Exception as e:
            log.warning("browser notice not sent: %s", e)

    async def _once(self, text: str) -> None:
        if not self.told:
            self.told = True
            await self._say(text)

    def _healthy(self) -> None:
        self.fails, self.checked, self.told, self.waited = 0, self.clock(), False, False

    async def ensure(self, me: str = "") -> bool:
        """Before browser work (me: the caller's own hold on the browser, see Friday._browser_busy).
        Enough checks to reach a decision now rather than fail the work first."""
        try:
            return self.fresh() or await self.check(me, FAILS_TO_RESTART)
        except Exception:  # callers hold the browser lock: a broken check must not take it down
            log.exception("browser check failed")
            return False

    async def check(self, me: str = "", tries: int = 1) -> bool:
        async with self._one:
            if self.fresh():  # another caller just checked or repaired it
                return True
            for _ in range(tries):
                result = await self._check()
                if result.ok:
                    self._healthy()
                    return True
                if result.unknown:
                    log.warning("browser check didn't run: %s", result.why)
                    return False
                self.fails += 1
                if self.fails >= FAILS_TO_RESTART:
                    break
            if self.fails < FAILS_TO_RESTART:
                return False
            if await self.busy(me):
                self.waited = True
                return False
            wait = float(self.store.get(RESTARTED_KEY) or 0) + RESTART_COOLDOWN - self.clock()
            if wait > 0:
                await self._once(f"⚠️ The browser is stuck again. I restarted it recently, so I'll try again "
                                 f"in about {max(1, round(wait / 60))} min.")
                return False
            self.store.set(RESTARTED_KEY, str(self.clock()))
            self.repairing = True
            try:
                why = await self.repair(me)
            except Exception as e:
                log.exception("browser restart failed")
                why = type(e).__name__
            finally:
                self.repairing = False
            if why:
                await self._once(f"⚠️ The browser is stuck and restarting it didn't help ({why[:120]}). "
                                 f"I'll try again in {RESTART_COOLDOWN // 60} min.")
                return False
            waited = self.waited
            self._healthy()
            await self._say("🔄 The browser was stuck, so I restarted it. Open tabs were closed; logins are kept."
                            + (" Browser work that was running when it got stuck may have failed." if waited else ""))
            return True


# --- tab leases ----------------------------------------------------------------------------------


def tabs_of(store, who: str) -> list[str]:
    """A task's tabs (CDP target ids), the one it used last at the end."""
    raw = store.get(LEASE + who) if who else None
    return json.loads(raw)["tabs"] if raw else []


def touch(store, who: str, *tabs: str) -> None:
    """Record tabs as this task's, the last of them as the one in use, and that it is active."""
    held = [t for t in tabs_of(store, who) if t not in tabs] + list(tabs)
    store.set(LEASE + who, json.dumps({"owner": who, "tabs": held, "at": time.time()}))


def release(store, who: str) -> None:
    """The task is over: sweep may close its tabs, and its next run gets a new one."""
    raw = store.get(LEASE + who)
    if raw:
        store.set(f"{RELEASED}{uuid.uuid4().hex}", raw)
        store.delete(LEASE + who)


def release_chats(store) -> None:
    for key, _ in leases(store).items():
        if key.startswith(LEASE + "chat:"):
            release(store, key.removeprefix(LEASE))


def leases(store) -> dict[str, dict]:
    rows = store.conn.execute("SELECT key, value FROM kv WHERE key LIKE ?", (LEASE + "%",)).fetchall()
    return {k: json.loads(v) for k, v in rows}


def plan_cleanup(held: dict[str, dict], live: dict[str, str], playing: set[str], active: set[str],
                 idle: float, now: float) -> tuple[set[str], list[str]]:
    """(tabs to close, leases that are over). A lease is due when released, or when its task isn't
    running and has been idle for `idle` seconds. Tabs playing media stay (their lease too, so
    they are looked at again), as do tabs another lease still holds. A tab in no lease belongs to
    nothing (restored by Chrome, left by a crash) and goes too, except a blank one."""
    due = {k: x for k, x in held.items()
           if k.startswith(RELEASED) or (x["owner"] not in active and now - x["at"] >= idle)}
    kept = {t for k, x in held.items() if k not in due for t in x["tabs"]} | playing
    close = {t for x in due.values() for t in x["tabs"] if t in live} - kept
    leased = {t for x in held.values() for t in x["tabs"]}
    close |= {t for t, url in live.items() if t not in leased and url != "about:blank"} - playing
    over = [k for k, x in due.items() if not ({t for t in x["tabs"] if t in live} - close)]
    return close, over


async def sweep(store, active, idle: float, busy, check=probe) -> int:
    """Close what no task holds any more, while no browser work runs (busy, asked again after the
    slow look at what's playing). active(): the tasks running now. How many it closed."""
    live = await open_tabs()
    if live is None:
        return 0
    close, over = plan_cleanup(leases(store), live, set(), active(), idle, time.time())
    if close:
        result = await check(sorted(close))
        if not result.ok or await busy():
            return 0
        close, over = plan_cleanup(leases(store), live, result.playing, active(), idle, time.time())
        if close:
            await close_tabs(close, live)
    for key in over:
        store.delete(key)
    return len(close)


if __name__ == "__main__" and sys.argv[1:2] == ["probe"]:
    asyncio.run(_look(sys.argv[2:]))
