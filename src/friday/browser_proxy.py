"""Between Codex and the Playwright MCP server: a stdio MCP server that relays every message to
the long-lived Playwright server (deploy/friday-playwright-mcp.service) and makes three things
better than the agent would on its own.

- A click that fails is diagnosed, not just reported: Friday looks at the element (where it is,
  whether something covers it, whether it's disabled or off screen) and does what fits: a mouse
  click at its position when it keeps moving (a video player, an animation), scrolling it into
  view, Escape for an overlay. Then it checks whether the page changed, and when an action failed
  or changed nothing the result carries a screenshot, so the agent sees what happened.
- browser_media: the state of the page's video and audio (all frames), and play/pause/seek with a
  verdict within a few seconds: playing, or why not (stuck loading, unsupported format, blocked).
- browser_evaluate (page JavaScript) runs only after the owner approves the exact script on
  Telegram, since a script could read what fill_login typed. The other blocked tools stay hidden.

Every forwarded call gets CALL_TIMEOUT seconds: past that the agent hears so and moves on. After a
navigation the agent's tab is brought to the front: Chrome throttles background tabs, which made
screenshots take seconds and can keep a video from loading at all.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
import sys
import time
from typing import Any

import httpx

from .browser import BLOCKED_TOOLS, CDP_URL

CALL_TIMEOUT = 30.0  # a forwarded browser step that takes longer is reported as failed
PLAY_VERDICT = 5.0  # seconds to decide whether playback really started
APPROVAL_TIMEOUT = 300
CLICKS = {"browser_click", "browser_hover"}
# After these the agent's tab may be another one: bring it to the front (Chrome.page).
FRONT_AFTER = {"browser_navigate", "browser_navigate_back", "browser_tabs", "browser_click"}
PROTOCOL = "2025-06-18"

MEDIA_TOOL = {
    "name": "browser_media",
    "description": (
        "The current tab's <video>/<audio> (inside frames too): position, duration, paused, "
        "loading, errors. action 'play' starts the main one and answers within ~5 seconds "
        "whether it is really playing, or why not (stuck loading: the site isn't sending the "
        "video; unsupported format; blocked). Also 'pause' and 'seek' (seconds). Use it "
        "instead of clicking a player's buttons and waiting to see."),
    "inputSchema": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["status", "play", "pause", "seek"]},
        "seconds": {"type": "number", "description": "for seek: the position"}},
        "required": ["action"]},
}
EVALUATE_TOOL = {
    "name": "browser_evaluate",
    "description": (
        "Run JavaScript in the current page, only when no other browser tool can do it. The "
        "owner must approve the exact script on Telegram first (it waits up to 5 minutes), so "
        "keep it short and say in 'reason' why. function: '() => { ... }' or, with target (a "
        "snapshot ref), "
        "'(element) => { ... }'."),
    "inputSchema": {"type": "object", "properties": {
        "function": {"type": "string"}, "reason": {"type": "string"},
        "element": {"type": "string"}, "target": {"type": "string"}},
        "required": ["function", "reason"]},
}

# Where the element is and what's in the way, from the element's own frame.
PROBE = """(e) => {
  const r = e.getBoundingClientRect(), x = r.x + r.width / 2, y = r.y + r.height / 2;
  const s = getComputedStyle(e), top = document.elementFromPoint(x, y);
  const name = (n) => n ? (n.tagName.toLowerCase() + (n.id ? '#' + n.id : '') +
    (n.getAttribute('aria-label') ? ' "' + n.getAttribute('aria-label').slice(0, 40) + '"' :
     n.className && typeof n.className === 'string' ? '.' + n.className.split(' ')[0] : '')) : '';
  return {x, y, w: r.width, h: r.height, vw: innerWidth, vh: innerHeight,
          hidden: s.visibility === 'hidden' || s.display === 'none' || r.width * r.height === 0,
          disabled: !!(e.disabled || e.getAttribute('aria-disabled') === 'true'),
          covered: !!top && top !== e && !e.contains(top) && !top.contains(e), by: name(top),
          framed: window !== window.top};
}"""
SCROLL = "(e) => { e.scrollIntoView({block: 'center', inline: 'center'}); return true; }"

MEDIA_STATE = """() => [...document.querySelectorAll('video, audio')].map((m, i) => {
  const r = m.getBoundingClientRect();
  return {i, tag: m.tagName.toLowerCase(), t: m.currentTime, d: m.duration, paused: m.paused,
          ended: m.ended, muted: m.muted, ready: m.readyState, net: m.networkState,
          error: m.error ? [m.error.code, m.error.message || ''] : null,
          buffered: m.buffered.length ? m.buffered.end(m.buffered.length - 1) : 0,
          area: r.width * r.height, src: (m.currentSrc || '').slice(0, 80)};
})"""
MEDIA_DO = """async ([i, action, seconds]) => {
  const m = document.querySelectorAll('video, audio')[i];
  if (!m) return 'gone';
  try {
    if (action === 'play') await m.play();
    else if (action === 'pause') m.pause();
    else if (action === 'seek') m.currentTime = seconds;
    return 'ok';
  } catch (e) { return e.name + ': ' + e.message; }
}"""
MEDIA_ERRORS = {1: "playback was aborted", 2: "a network error stopped the download",
                3: "the file couldn't be decoded", 4: "this format isn't supported here"}


def _text(result: dict) -> str:
    return "\n".join(c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text")


def _result(text: str, error: bool = False, images: list[dict] | None = None) -> dict:
    return {"content": [{"type": "text", "text": text}, *(images or [])], "isError": error}


def click_plan(probe: dict | None, error: str) -> tuple[str, str]:
    """(remedy, why) for a click that failed. remedy: xy | scroll | escape | refresh | none."""
    if "waiting for locator" in error and "resolved to" not in error or probe is None:
        return "refresh", "the element isn't on the page any more (the page changed): take a new snapshot"
    if probe.get("hidden"):
        return "none", "the element is hidden or has no size"
    if probe.get("disabled"):
        return "none", "the element is disabled"
    off = not (0 <= probe["x"] <= probe["vw"] and 0 <= probe["y"] <= probe["vh"])
    if off:
        return "scroll", "the element is outside the visible area"
    if probe.get("covered") or "intercepts pointer events" in error:
        return "escape", f"something covers it ({probe.get('by') or 'an overlay'})"
    return "xy", "the element keeps moving or changing (an animation, a loading player)"


def media_verdict(before: dict, after: dict, play_said: str) -> tuple[bool, str]:
    """Whether play worked, from the element's state before and PLAY_VERDICT seconds after."""
    if after.get("t", 0) - before.get("t", 0) > 0.5 and not after.get("paused"):
        return True, f"playing: {after['t']:.0f}s of {after.get('d') or 0:.0f}s"
    if after.get("error"):
        code, msg = after["error"]
        return False, f"media error {code}: {MEDIA_ERRORS.get(code, '')} {msg}".strip()
    if play_said.startswith("NotAllowedError"):
        return False, "the browser blocked playback without a click on the page"
    if play_said not in ("ok", ""):
        return False, f"play() failed: {play_said}"
    if after.get("paused"):
        return False, "the page paused it again (an ad, a consent dialog or a login wall?)"
    if after.get("ready", 0) < 3:
        got = after.get("buffered", 0)
        return False, (f"stuck loading at {after.get('t', 0):.0f}s: no video data is arriving "
                       f"(buffered {got:.0f}s). The site isn't sending it to this browser; "
                       "waiting longer won't help")
    return False, "it didn't move although it isn't paused"


class Upstream:
    """The Playwright MCP server over streamable HTTP, one session."""

    def __init__(self, url: str):
        self.url, self.session, self.version = url, "", PROTOCOL
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(CALL_TIMEOUT + 10, connect=5))
        self.ids = itertools.count(1)

    def headers(self) -> dict[str, str]:
        h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if self.session:
            h["Mcp-Session-Id"] = self.session
            h["MCP-Protocol-Version"] = self.version
        return h

    async def send(self, msg: dict, on_message) -> dict | None:
        """POST one message; on_message gets what the server sends besides the answer (its own
        requests, notifications). Returns the answer to msg (None for a notification)."""
        async with self.http.stream("POST", self.url, json=msg, headers=self.headers()) as r:
            if sid := r.headers.get("mcp-session-id"):
                self.session = sid
            if r.status_code == 202 or "id" not in msg:
                return None
            if r.status_code >= 400:
                body = (await r.aread()).decode(errors="replace")[:300]
                return {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32000, "message": f"HTTP {r.status_code}: {body}"}}
            if "text/event-stream" not in r.headers.get("content-type", ""):
                return json.loads(await r.aread())
            data: list[str] = []
            async for line in r.aiter_lines():
                if line.startswith("data:"):
                    data.append(line[5:].strip())
                elif not line and data:
                    out = json.loads("\n".join(data))
                    data = []
                    if out.get("id") == msg["id"] and ("result" in out or "error" in out):
                        return out
                    await on_message(out)
        return {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32000, "message": "no answer"}}

    async def call(self, name: str, args: dict, on_message, timeout: float = CALL_TIMEOUT) -> dict:
        """A tool call of Friday's own (its ids never clash with Codex's)."""
        msg = {"jsonrpc": "2.0", "id": f"friday-{next(self.ids)}", "method": "tools/call",
               "params": {"name": name, "arguments": args}}
        try:
            out = await asyncio.wait_for(self.send(msg, on_message), timeout)
        except TimeoutError:
            return _result(f"No answer from the browser in {timeout:.0f}s.", True)
        return out.get("result") or _result(str((out.get("error") or {}).get("message")), True)


class Chrome:
    """Friday's own connection to the shared browser (CDP), kept for the proxy's life: screenshots,
    mouse and keys without the Playwright server's waiting, and bringing the agent's tab to the
    front. A background tab is throttled: screenshots take seconds and video may not load."""

    def __init__(self):
        self.pw = self.browser = None
        self.sessions: dict = {}

    async def page(self, url: str):
        if self.browser is None or not self.browser.is_connected():
            from playwright.async_api import async_playwright

            if self.pw is None:
                self.pw = await async_playwright().start()
            self.browser = await self.pw.chromium.connect_over_cdp(CDP_URL)
            self.sessions = {}
        pages = [pg for c in self.browser.contexts for pg in c.pages]
        return next((pg for pg in pages if pg.url == url), None) or next(
            (pg for pg in pages if url and pg.url.split("#")[0] == url.split("#")[0]), None)

    async def cdp(self, page, method: str, params: dict | None = None) -> dict:
        if page not in self.sessions:
            self.sessions[page] = await page.context.new_cdp_session(page)
        return await self.sessions[page].send(method, params or {})

    async def shot(self, page) -> dict:
        r = await self.cdp(page, "Page.captureScreenshot", {"format": "jpeg", "quality": 60, "optimizeForSpeed": True})
        return {"type": "image", "data": r["data"], "mimeType": "image/jpeg"}


class Relay:
    def __init__(self, url: str):
        self.up = Upstream(url)
        self.chrome = Chrome()
        self.out_lock = asyncio.Lock()
        self.tasks: set[asyncio.Task] = set()

    async def write(self, msg: dict) -> None:
        async with self.out_lock:
            sys.stdout.write(json.dumps(msg) + "\n")
            sys.stdout.flush()

    async def passthrough(self, msg: dict) -> None:
        """What the Playwright server sends besides answers goes to Codex as is."""
        await self.write(msg)

    async def handle(self, msg: dict) -> None:
        method = msg.get("method")
        try:
            if method == "tools/call":
                out = {"jsonrpc": "2.0", "id": msg["id"], "result": await self.tool(msg)}
            else:
                out = await self.up.send(msg, self.passthrough)
                if method == "initialize" and out and "result" in out:
                    self.up.version = out["result"].get("protocolVersion") or PROTOCOL
                if method == "tools/list" and out and "result" in out:
                    tools = [t for t in out["result"].get("tools") or [] if t.get("name") not in BLOCKED_TOOLS]
                    out["result"]["tools"] = tools + [MEDIA_TOOL, EVALUATE_TOOL]
        except Exception as e:
            out = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32000, "message": str(e)}} if "id" in msg else None
        if out is not None and "id" in msg:
            await self.write(out)

    async def tool(self, msg: dict) -> dict:
        params = msg.get("params") or {}
        name, args = params.get("name", ""), params.get("arguments") or {}
        if name == "browser_media":
            return await self.media(str(args.get("action") or "status"), args.get("seconds"))
        if name == "browser_evaluate":
            return await self.evaluate(args)
        if name in BLOCKED_TOOLS:
            return _result(f"{name} isn't available.", True)
        timeout = 120.0 if name == "browser_wait_for" else CALL_TIMEOUT
        fwd = {**msg, "params": {**params, "arguments": args}}
        try:
            out = await asyncio.wait_for(self.up.send(fwd, self.passthrough), timeout)
        except TimeoutError:
            return _result(f"{name} got no answer from the browser in {timeout:.0f}s and was abandoned. "
                           "Don't repeat it as is: look at the page (screenshot) and try another way.", True,
                           await self.screenshot())
        result = out.get("result") if out else None
        if result is None:
            return _result(str(((out or {}).get("error") or {}).get("message") or "no answer"), True)
        if name in FRONT_AFTER:
            await self.front()
        error = _text(result)
        if name in CLICKS and result.get("isError") and args.get("target") and (
                "Timeout" in error or "intercepts pointer events" in error):
            return await self.fix_click(name, args, result)
        return result

    async def front(self):
        """The agent's current tab, brought to the front (None if it can't be found)."""
        try:
            async with asyncio.timeout(8):
                page = await self.chrome.page(await self.current_url())
                if page is not None:
                    await self.chrome.cdp(page, "Page.bringToFront")
                return page
        except Exception:
            return None

    # --- clicks that fail -------------------------------------------------------------------

    async def screenshot(self, page=None) -> list[dict]:
        try:
            async with asyncio.timeout(8):
                page = page or await self.front()
                if page is not None:
                    return [await self.chrome.shot(page)]
        except Exception:
            pass
        r = await self.up.call("browser_take_screenshot", {"type": "jpeg", "scale": "css"}, self.passthrough,
                               timeout=10)
        return [c for c in r.get("content") or [] if c.get("type") == "image"]

    async def probe(self, args: dict) -> dict | None:
        r = await self.up.call("browser_evaluate", {"function": PROBE, "element": args.get("element", ""),
                                                    "target": args["target"]}, self.passthrough, timeout=8)
        if r.get("isError"):
            return None
        m = re.search(r"### Result\s*\n(.*?)(?:\n###|\Z)", _text(r), re.S)
        try:
            return json.loads(m.group(1).strip()) if m else None
        except ValueError:
            return None

    async def fix_click(self, name: str, args: dict, failed: dict) -> dict:
        error = _text(failed)
        page = await self.front()
        before = await self.screenshot(page)
        probe = await self.probe(args)
        remedy, why = click_plan(probe, error)
        done = ""
        if remedy == "scroll":
            await self.up.call("browser_evaluate", {"function": SCROLL, "element": args.get("element", ""),
                                                    "target": args["target"]}, self.passthrough, timeout=8)
            probe = await self.probe(args)
            remedy, done = ("xy", "scrolled it into view, ") if probe and not probe.get("hidden") else ("none", "")
        if remedy == "escape":
            if page is not None:
                await page.keyboard.press("Escape")
            else:
                await self.up.call("browser_press_key", {"key": "Escape"}, self.passthrough, timeout=8)
            probe = await self.probe(args)
            remedy, done = ("xy", "pressed Escape, ") if probe and not probe.get("covered") else ("none", "pressed Escape (still covered), ")
        if remedy == "xy" and probe and probe.get("framed"):
            remedy, why = "none", why + "; it is inside a frame, so its position on the page is unknown"
        if remedy != "xy" or not probe:
            note = f"Friday: the {name.removeprefix('browser_')} failed: {why}. {done}Nothing else was tried."
            return _result(f"{error.strip()[:600]}\n\n{note} The screenshot shows the page now.", True,
                           await self.screenshot(page))
        x, y = round(probe["x"]), round(probe["y"])
        if page is not None:  # straight to the browser: the Playwright server waits for the page to settle
            r = {"content": [{"type": "text", "text": ""}]}
            try:
                await (page.mouse.click(x, y) if name == "browser_click" else page.mouse.move(x, y))
            except Exception as e:
                r = _result(str(e), True)
        else:
            tool = "browser_mouse_click_xy" if name == "browser_click" else "browser_mouse_move_xy"
            r = await self.up.call(tool, {"x": x, "y": y}, self.passthrough, timeout=10)
        await asyncio.sleep(0.6)
        after = await self.screenshot(page)
        changed = bool(before and after and before[0].get("data") != after[0].get("data"))
        what = f"Friday: {why}, so I {done}clicked it with the mouse at its position ({probe['x']:.0f}, {probe['y']:.0f})"
        if r.get("isError"):
            return _result(f"{what}, and that failed too: {_text(r)[:300]}", True, after)
        if changed:
            return _result(f"{what}. The page changed afterwards; take a snapshot for the new refs.")
        return _result(f"{what}, but the page looks the same afterwards: check the screenshot before "
                       "trying again.", True, after)

    # --- media ---------------------------------------------------------------------------

    async def current_url(self) -> str:
        r = await self.up.call("browser_tabs", {"action": "list"}, self.passthrough, timeout=8)
        m = re.search(r"\(current\)[^\n]*?\]\((\S+?)\)", _text(r))
        return m.group(1) if m else ""

    async def media(self, action: str, seconds: Any = None) -> dict:
        page = await self.front()  # a background tab may not load its video at all
        if page is None:
            return _result("Couldn't find the current tab in the browser.", True)
        found = []
        for frame in page.frames:
            try:
                for m in await frame.evaluate(MEDIA_STATE):
                    found.append((frame, m))
            except Exception:
                continue
        if not found:
            return _result("There is no <video> or <audio> on this page yet (the player may start "
                           "after a click, or draw on a canvas).", action != "status")
        frame, m = max(found, key=lambda fm: (fm[1]["area"], fm[1]["d"] or 0))
        if action == "status":
            return _result(json.dumps([mm for _, mm in found], ensure_ascii=False))
        said = await frame.evaluate(MEDIA_DO, [m["i"], action, float(seconds or 0)])
        if action != "play":
            state = (await frame.evaluate(MEDIA_STATE))[m["i"]]
            return _result(f"{action}: {said}; now at {state['t']:.0f}s, paused={state['paused']}",
                           said != "ok")
        # Answer as soon as it moves; a stall takes the whole PLAY_VERDICT to call.
        deadline = time.monotonic() + PLAY_VERDICT
        while True:
            await asyncio.sleep(0.5)
            after = (await frame.evaluate(MEDIA_STATE))[m["i"]]
            ok, verdict = media_verdict(m, after, said)
            if ok or time.monotonic() >= deadline:
                break
        return _result(("▶️ " if ok else "⛔ ") + verdict, not ok)

    # --- page JavaScript, with the owner's approval --------------------------------------

    async def evaluate(self, args: dict) -> dict:
        from .config import load_config
        from .db import Store

        code = str(args.get("function") or "")
        url = await self.current_url()
        store = Store(load_config().db_path)
        text = (f"🧩 Run this JavaScript on {url or 'the current page'}?\n"
                f"Why: {str(args.get('reason') or '-')[:300]}\n\n{code[:1500]}")
        aid = store.add_approval("action", text)
        store.queue("approval", text=str(aid))
        deadline = time.monotonic() + APPROVAL_TIMEOUT
        status = "pending"
        while time.monotonic() < deadline and status == "pending":
            await asyncio.sleep(2)
            status = store.approval(aid)["status"]
        if status == "pending":
            store.decide_approval(aid, "expired")
        if status != "approved":
            return _result(f"Not run: the owner {'denied it' if status == 'denied' else 'did not answer'}.", True)
        fwd = {k: args[k] for k in ("function", "element", "target") if args.get(k)}
        return await self.up.call("browser_evaluate", fwd, self.passthrough)

    # --- stdio -----------------------------------------------------------------------------

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        warm = asyncio.create_task(self.chrome.page(""))  # connecting takes ~2s: do it before it's needed
        warm.add_done_callback(lambda t: t.exception())
        reader = asyncio.StreamReader(limit=1 << 25)
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        while line := await reader.readline():
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            task = asyncio.create_task(self.handle(msg))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)


def main() -> None:
    asyncio.run(Relay(os.environ["FRIDAY_PLAYWRIGHT_URL"]).run())


if __name__ == "__main__":
    main()
