"""Between Codex and the Playwright MCP server: a stdio MCP server that relays every message to
the long-lived Playwright server (deploy/friday-playwright-mcp.service) and makes three things
better than the agent would on its own.

- A click that fails is diagnosed, not just reported: Friday looks at the element (where it is,
  whether something covers it, whether it's disabled or off screen) and does what fits: a mouse
  click at its position when it keeps moving (a video player, an animation), scrolling it into
  view, Escape for an overlay. Then it checks whether the page changed, and when an action failed
  or changed nothing the result carries a screenshot, so the agent sees what happened.
- browser_media reads the page's video and audio (all frames): position, rate, and whether it's
  really advancing. It only reads: a player is operated through its own controls like any page.
- browser_evaluate (page JavaScript) runs only after the owner approves the exact script on
  Telegram, since a script could read what fill_login typed. The other blocked tools stay hidden.
- Every action's result says what it did: [effect: changed | unchanged | unknown], from the URL,
  tabs, dialogs, visible text, controls and media, ignoring a move of keyboard focus. Where the page
  can't tell (a canvas, a frame it can't read) the screen is compared, and an animated one is
  unknown. Only hashes are compared; what's typed is never kept.
- It also lists the controls the action made appear or go away on that page (a menu, a control bar
  under the pointer, a dialog), with refs. Nothing is hovered or opened on the agent's behalf.
- A control can be on screen and not in the page's structure (a canvas, an image button, a built-in
  player). When a lookup finds nothing, the page has such regions, or an action changed nothing, the
  result carries a screenshot to open. Where to hover or click stays the agent's choice.

Every forwarded call gets CALL_TIMEOUT seconds: past that the agent hears so and moves on.

Each task works in its own tab (browser_care leases). Playwright MCP 0.0.84 starts a session on the
browser's first tab, doesn't switch to a popup, and when the current tab closes moves to its
neighbour, which may be another task's. So the session is put on the task's tab before its first
call and after any of that, and tabs the task opens join its lease. The tab is brought to the front
before each call and after a navigation: Chrome throttles background tabs, which made clicks time
out and screenshots take seconds, and can keep a video from loading at all.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import itertools
import json
import os
import re
import signal
import sys
import time
import uuid
from collections import Counter
from contextlib import suppress
from math import isfinite

import httpx

from . import browser_care as care
from .browser import BLOCKED_TOOLS, CDP_URL

OUTPUT_LIMIT = 20_000  # characters of one tool result that reach the agent; the full text goes to a file
CALL_TIMEOUT = 30.0  # a forwarded browser step that takes longer is reported as failed
MEDIA_SAMPLE = 0.6  # seconds between two readings of a player, to tell whether it's really advancing
CHANGE_SHOWN = 14  # controls listed as having appeared after an action; the rest are counted
APPROVAL_TIMEOUT = 300
TAB_TIMEOUT = 20.0  # finding or opening the task's tab
LEASE_TOUCH = 60  # seconds between notes that the task still uses its tabs
CLICKS = {"browser_click", "browser_hover"}
# Tools that act on the page: their results say whether anything changed.
ACTIONS = {f"browser_{t}" for t in (
    "click", "hover", "check", "uncheck", "drag", "drop", "type", "press_key", "press_sequentially", "keydown", "keyup",
    "fill_form", "select_option", "file_upload", "navigate", "navigate_back", "navigate_forward",
    "reload", "mouse_click_xy", "mouse_move_xy", "mouse_down", "mouse_up", "mouse_drag_xy", "mouse_wheel")}
LOOKUPS = {"browser_find", "browser_snapshot"}
STATE_SECONDS = 2.5  # reading the page's state around an action; longer means "unknown"
MAX_FRAMES = 12
SCREEN_SETTLE = 0.35  # a screen that still differs after this is animating
EFFECT_LINES = {
    "changed": "[effect: changed]",
    "unchanged": "[effect: unchanged] The page is the same after this action. Find out why before repeating it.",
    "unknown": "[effect: unknown] Friday can't tell whether this changed anything: look at the page.",
    "focus": "[effect: unchanged] Only the keyboard focus moved.",
}
SEE_IT = ("Friday: {why} If what you need is visible in this screenshot, act on it by position (browser_mouse_click_xy, "
          "or browser_mouse_move_xy to hover), then check the result.")
SNAPSHOT_FILE = re.compile(r"\[Snapshot\]\((\S+?\.yml)\)")
PAGE_URL = re.compile(r"^- Page URL: (\S+)", re.M)
# A line of Playwright's page structure: role, "name", [attributes], and any text after the colon.
TREE_LINE = re.compile(r"""^\s*- '?(\w+)(?: "((?:[^"\\]|\\.)*)")?((?: \[[^\]]*\])*):?[ ]?(.*)$""", re.M)
CONTROL_ROLES = {"button", "link", "menu", "menuitem", "menuitemradio", "menuitemcheckbox", "dialog", "alertdialog", "alert",
                 "slider", "tab", "textbox", "searchbox", "combobox", "listbox", "option", "checkbox", "radio", "switch",
                 "spinbutton"}
# A click may open a popup, which Chrome puts in front: the agent's tab goes back there.
FRONT_AFTER = {"browser_navigate", "browser_navigate_back", "browser_tabs", "browser_click"}
PROTOCOL = "2025-06-18"
TAB_LINE = re.compile(r"^- (\d+):( \(current\))? \[.*\]\((\S*)\)(?: \[[^\]\n]*\])*$", re.M)

MEDIA_TOOL = {
    "name": "browser_media",
    "description": (
        "Read the current tab's <video>/<audio> (inside frames too): position, duration, playback "
        "rate, muted, loading, errors, and whether it's really advancing. It only reads: operate a "
        "player through its own controls, then use this to check what happened."),
    "inputSchema": {"type": "object", "properties": {}},
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
# What an action can change in a frame, as hashes computed in the page, so that what's typed in a
# field never leaves it. A password field counts by length only. opaque: regions the page's text
# and structure can't describe.
PAGE_STATE = """() => {
  const h = (s) => { let x = 0x811c9dc5; for (let i = 0; i < s.length; i++) { x ^= s.charCodeAt(i); x = Math.imul(x, 16777619); } return x >>> 0; };
  const a = (e, n) => e.getAttribute(n) || '';
  const value = (e) => e.value == null ? '' : e.type === 'password' ? 'len' + String(e.value).length : h(String(e.value));
  const controls = [...document.querySelectorAll('input,select,textarea,button,details,dialog,[role],[aria-expanded],[contenteditable]')]
    .slice(0, 4000).map((e) => [e.tagName, e.type || '', e.disabled ? 1 : 0, e.checked ? 1 : 0, e.open ? 1 : 0, e.selectedIndex ?? '',
      value(e), a(e, 'aria-expanded'), a(e, 'aria-checked'), a(e, 'aria-selected'), a(e, 'aria-pressed'), e.hidden ? 1 : 0].join(':')).join('|');
  const media = [...document.querySelectorAll('video,audio')].map((m) => [m.currentSrc, m.paused, m.ended].join(':')).join('|');
  const scrolled = [...document.querySelectorAll('div,section,main,ul,ol,table,tbody,aside,nav,article')].slice(0, 5000)
    .filter((e) => e.scrollTop || e.scrollLeft).map((e) => e.scrollTop + ',' + e.scrollLeft).join('|');
  const area = innerWidth * innerHeight, shown = (e, part) => {
    const r = e.getBoundingClientRect();
    return r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth && r.width * r.height >= area * part;
  };
  const bare = (e) => { const c = e.closest('a,button,[role=button],[onclick]');
    return !!c && !a(e, 'alt') && !a(c, 'aria-label') && !(c.innerText || '').trim(); };
  const opaque = [...document.querySelectorAll('canvas,iframe,embed,object,video[controls]')].some((e) => shown(e, 0.1))
    || [...document.querySelectorAll('audio[controls]')].some((e) => shown(e, 0.001))
    || [...document.querySelectorAll('img,input[type=image]')].slice(0, 500).some((e) => shown(e, 0.1) || (bare(e) && shown(e, 0.001)));
  return {page: h([location.href, document.body ? document.body.innerText : '', controls, media].join('|')),
          scroll: scrollX + ',' + scrollY + ';' + scrolled, opaque};
}"""

MEDIA_STATE = """() => [...document.querySelectorAll('video, audio')].map((m, i) => {
  const r = m.getBoundingClientRect();
  return {i, tag: m.tagName.toLowerCase(), t: m.currentTime, d: m.duration, rate: m.playbackRate, paused: m.paused,
          ended: m.ended, muted: m.muted, ready: m.readyState,
          error: m.error ? [m.error.code, m.error.message || ''] : null,
          buffered: m.buffered.length ? m.buffered.end(m.buffered.length - 1) : 0,
          area: r.width * r.height};
})"""
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
    return "xy", "the click didn't reach it (it may be moving or changing)"


def action_effect(before: dict | None, after: dict | None, again: str | None = None, focus_only: bool = False) -> str:
    """changed | unchanged | unknown | focus (EFFECT_LINES), from the page's state around an action.
    again: the screen a moment after `after`, when the screen had to decide. focus_only: the key
    only moves focus, so what that scrolls into view doesn't count."""
    if not before or not after:
        return "unknown"
    if before["url"] != after["url"] or before["tabs"] != after["tabs"] or before["dom"] != after["dom"]:
        return "changed"
    if focus_only:
        return "focus"
    if before["scroll"] != after["scroll"]:
        return "changed"
    if not before["opaque"] and not before["blind"] and not after["blind"]:
        return "unchanged"
    if not before.get("shot") or not after.get("shot"):
        return "unknown"
    if before["shot"] == after["shot"]:
        return "unchanged"
    return "changed" if again == after["shot"] else "unknown"  # still moving: an animation or a video


def with_line(result: dict, line: str) -> dict:
    """The result with a line of Friday's first."""
    return {**result, "content": [{"type": "text", "text": line}, *(result.get("content") or [])]}


def images_to_files(result: dict, save) -> dict:
    """Screenshots as files the agent can open. Codex hands a tool result to the model as text, so an
    attached image would arrive as base64 it can't see. save(image part) -> the file's path, or ""."""
    content = result.get("content") or []
    if not any(c.get("type") == "image" for c in content):
        return result
    out = []
    for c in content:
        if c.get("type") != "image":
            out.append(c)
            continue
        path = save(c)
        out.append({"type": "text", "text": f"[Screenshot saved: {path}. To see it, open that file with view_image.]"
                    if path else "[Friday couldn't save this screenshot.]"})
    return {**result, "content": out}


def bounded(result: dict, save, limit: int = OUTPUT_LIMIT) -> dict:
    """A result that fits the limit: the start and end of its longest text, cut at line breaks, and
    where the whole of it was saved. save(text) -> the file's path, or "" if it couldn't be saved."""
    content = result.get("content") or []
    texts = [c for c in content if c.get("type") == "text"]
    total = sum(len(c.get("text", "")) for c in texts)
    if total <= limit:
        return result
    path = save("\n".join(c.get("text", "") for c in texts))
    where = f"The full output is in {path}. " if path else ""
    note = (f"\n… [Friday cut this output: {total} characters. {where}To see a part, use browser_find, "
            "browser_snapshot with a target ref" + (", or read that file" if path else "") + ".]\n")
    longest = max(texts, key=lambda c: len(c.get("text", "")))
    room = max(limit - (total - len(longest["text"])) - len(note), 2000)
    text = longest["text"]
    head, tail = text[:room * 3 // 4], text[-(room // 4):]
    head = head[:head.rfind("\n")] if "\n" in head else head
    tail = tail[tail.find("\n") + 1:] if "\n" in tail else tail
    return {**result, "content": [{**c, "text": head + note + tail} if c is longest else c for c in content]}


def media_note(before: dict, after: dict) -> str:
    """What a player is doing, from two readings MEDIA_SAMPLE seconds apart."""
    if after.get("error"):
        code, msg = after["error"]
        return f"media error {code}: {MEDIA_ERRORS.get(code, '')} {msg}".strip()
    if after.get("ended"):
        return "ended"
    if after.get("paused"):
        return "paused"
    if after.get("t", 0) > before.get("t", 0):
        return "playing"
    if after.get("ready", 0) < 3:
        return f"not advancing: waiting for data (buffered to {after.get('buffered', 0):.0f}s)"
    return "not advancing although it isn't paused"


def _controls(tree: str) -> list[tuple[str, str]]:
    """The controls in a page structure, as (what identifies it, how to show it). Numbers don't count
    towards identity, so a running clock or counter in a label isn't a new control."""
    out = []
    for role, name, attrs, text in TREE_LINE.findall(tree):
        ref = re.search(r"\[ref=([\w-]+)\]", attrs)
        label = (name or text).strip().strip("'\"")[:50]
        if ref and (role in CONTROL_ROLES or ("[cursor=pointer]" in attrs and label)):
            out.append((f"{role} {re.sub(r'[0-9]+', '#', label)}", f'{role} "{label}" [ref={ref[1]}]' if label else f"{role} [ref={ref[1]}]"))
    return out


def structure_change(before: str, after: str, shown: int = CHANGE_SHOWN) -> str:
    """What an action made appear and go away among a page's controls, with refs to act on."""
    def extra(items, other):
        left, out = Counter(k for k, _ in other), []
        for key, label in items:
            if left[key] > 0:
                left[key] -= 1
            else:
                out.append(label)
        return out

    old, new = _controls(before), _controls(after)
    came, gone = extra(new, old), [x.split(" [ref=")[0] for x in extra(old, new)]
    lines = []
    if came:
        lines.append("[appeared: " + "; ".join(came[:shown]) + (f"; and {len(came) - shown} more" if len(came) > shown else "") + "]")
    if gone:
        lines.append("[went away: " + "; ".join(gone[:6]) + (f"; and {len(gone) - 6} more" if len(gone) > 6 else "") + "]")
    return "\n".join(lines)


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

    async def close(self) -> None:
        """End the session: the server keeps a session's connection to Chrome until told. Its tabs stay."""
        if self.session:
            with suppress(Exception):
                await asyncio.wait_for(self.http.delete(self.url, headers=self.headers()), 5)
            self.session = ""


class Chrome:
    """Friday's own connection to the shared browser (CDP), kept for the proxy's life: screenshots,
    mouse and keys without the Playwright server's waiting, and bringing the agent's tab to the
    front. A background tab is throttled: screenshots take seconds and video may not load."""

    def __init__(self):
        self.pw = self.browser = self.root = None
        self.sessions: dict = {}

    async def connect(self):
        if self.browser is None or not self.browser.is_connected():
            from playwright.async_api import async_playwright

            if self.pw is None:
                self.pw = await async_playwright().start()
            self.browser = await self.pw.chromium.connect_over_cdp(CDP_URL)
            self.sessions, self.root = {}, None
        return self.browser

    async def pages(self) -> list:
        return [pg for c in (await self.connect()).contexts for pg in c.pages]

    async def targets(self) -> dict[str, dict]:
        """Open tabs: CDP target id -> its info (url, openerId)."""
        browser = await self.connect()
        if self.root is None:
            self.root = await browser.new_browser_cdp_session()
        infos = (await self.root.send("Target.getTargets"))["targetInfos"]
        return {t["targetId"]: t for t in infos if t["type"] == "page"}

    async def target_of(self, page) -> str:
        return (await self.cdp(page, "Target.getTargetInfo"))["targetInfo"]["targetId"]

    async def page_of(self, target: str):
        for page in await self.pages():
            try:
                if await self.target_of(page) == target:
                    return page
            except Exception:  # a tab that went away
                self.sessions.pop(page, None)
        return None

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
        self.owner = os.environ.get(care.OWNER_ENV) or f"session:{os.getpid()}"
        self.tab = None  # the Playwright page this session is on
        self.tab_id = ""
        self.tab_lock = asyncio.Lock()
        self.known: set[str] = set()  # tabs seen so far: any other is new
        self.touched = 0.0
        self._store = None
        self.seen_pages: set[str] = set()  # pages whose screenshot a lookup already carried
        self.shown_state: tuple | None = None
        self.tree: tuple[str, str] | None = None  # (url, page structure) after the last action or full snapshot

    @property
    def store(self):
        if self._store is None:
            from .config import load_config
            from .db import Store

            self._store = Store(load_config().db_path)
        return self._store

    def _output_path(self, suffix: str):
        """Where a long result or a screenshot goes: the job's temporary folder, or the browser's files."""
        from .config import load_config

        cfg = load_config()
        job = re.fullmatch(r"job:(\d+)", self.owner)
        folder = cfg.workspace / "jobs" / job[1] if job else cfg.files_dir / "browser"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"page-{uuid.uuid4().hex[:8]}{suffix}"

    def save_output(self, text: str) -> str:
        try:
            path = self._output_path(".txt")
            path.write_text(text, encoding="utf-8")
            return str(path)
        except OSError:
            return ""

    def save_image(self, part: dict) -> str:
        try:
            path = self._output_path(".png" if "png" in str(part.get("mimeType")) else ".jpg")
            path.write_bytes(base64.b64decode(part.get("data") or "", validate=True))
            return str(path)
        except (OSError, ValueError):
            return ""

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
                result = images_to_files(await self.tool(msg), self.save_image)
                out = {"jsonrpc": "2.0", "id": msg["id"], "result": bounded(result, self.save_output)}
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
        if name in BLOCKED_TOOLS and name != "browser_evaluate":
            return _result(f"{name} isn't available.", True)
        if problem := await self.use_tab():
            return _result(problem, True)
        if name == "browser_media":
            return await self.media()
        if name == "browser_evaluate":
            return await self.evaluate(args)
        if name in ACTIONS:
            before = await self.page_state()
            result = await self.forward(msg, name, args)
            return await self.with_change(await self.with_effect(name, args, result, before))
        result = await self.forward(msg, name, args)
        if name == "browser_snapshot" and not result.get("isError") and not args.get("target") and not args.get("depth"):
            text = _text(result)
            self.tree = ((m[1] if (m := PAGE_URL.search(text)) else ""), text)
        return await self.with_view(name, result) if name in LOOKUPS else result

    async def forward(self, msg: dict, name: str, args: dict) -> dict:
        params = msg.get("params") or {}
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
        await self.note_tabs(str(args.get("action") or "") if name == "browser_tabs" else None,
                             name == "browser_close")
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
                page = self.tab if self.tab is not None and not self.tab.is_closed() else None
                if page is not None:
                    await self.chrome.cdp(page, "Page.bringToFront")
                return page
        except Exception:
            return None

    # --- what an action did, and what the text can't show -------------------------------------

    async def page_state(self, shot: bool | None = None) -> dict | None:
        """The current tab's state (hashes), or None when it can't be read in time. shot: with a hash
        of the screen; by default when the page has regions its structure can't describe."""
        page = self.tab if self.tab is not None and not self.tab.is_closed() else None
        if page is None:
            return None
        try:
            async with asyncio.timeout(STATE_SECONDS):
                frames, blind = [], len(page.frames) > MAX_FRAMES
                for frame in page.frames[:MAX_FRAMES]:
                    try:
                        frames.append(await frame.evaluate(PAGE_STATE))
                    except TimeoutError:
                        raise
                    except Exception:  # a frame going away, or one that can't be read
                        blind = True
                if not frames:
                    return None
                state = {"url": page.url, "tabs": sorted(await self.chrome.targets()), "blind": blind,
                         "dom": tuple(x["page"] for x in frames), "scroll": tuple(x["scroll"] for x in frames),
                         "opaque": any(x["opaque"] for x in frames), "shot": None}
                if state["opaque"] or blind if shot is None else shot:
                    state["shot"] = await self.screen_hash(page)
                return state
        except Exception:  # includes a dialog holding the page
            return None

    async def screen_hash(self, page) -> str:
        return hashlib.sha256((await self.chrome.shot(page))["data"].encode()).hexdigest()

    async def with_effect(self, name: str, args: dict, result: dict, before: dict | None) -> dict:
        text = _text(result)
        if "### Modal state" in text or "Downloading file" in text or "Downloaded file" in text:
            return with_line(result, EFFECT_LINES["changed"])  # a dialog holds the page: it can't be read
        after = await self.page_state(bool(before and before["shot"]))
        again = None
        if before and after and before["shot"] and after["shot"] and before["shot"] != after["shot"]:
            await asyncio.sleep(SCREEN_SETTLE)
            page = self.tab if self.tab is not None and not self.tab.is_closed() else None
            with suppress(Exception):
                async with asyncio.timeout(STATE_SECONDS):
                    again = await self.screen_hash(page) if page is not None else None
        focus = name == "browser_press_key" and str(args.get("key") or "").lower() in ("tab", "shift+tab")
        effect = action_effect(before, after, again, focus)
        out = with_line(result, EFFECT_LINES[effect])
        if effect == "unchanged" and not result.get("isError"):
            return await self.with_screen(out, "This action changed nothing on the page.", after)
        return out

    def read_tree(self, text: str) -> str | None:
        """The page structure Playwright saved after an action (its result links to the file)."""
        link = SNAPSHOT_FILE.search(text)
        if not link:
            return None
        try:
            from .config import load_config

            root = load_config().workspace.resolve()
            path = (root / link[1]).resolve()
            return path.read_text(encoding="utf-8", errors="replace") if path.is_relative_to(root) else None
        except OSError:
            return None

    async def with_change(self, result: dict) -> dict:
        """An action's result, plus what it made appear or go away on the same page: a menu that
        opened, a control bar that showed up under the pointer, a dialog. The agent decides what
        to do with it. A new page has no comparison: the agent reads it."""
        if result.get("isError"):
            return result
        tree = self.read_tree(_text(result))
        if tree is None:  # mouse moves and scrolls don't save one
            snap = await self.up.call("browser_snapshot", {}, self.passthrough, timeout=5)
            if snap.get("isError"):
                return result
            tree = _text(snap)
        url = m[1] if (m := PAGE_URL.search(_text(result)) or PAGE_URL.search(tree)) else ""
        previous, self.tree = self.tree, (url, tree)
        if not previous or previous[0] != url:
            return result
        change = structure_change(previous[1], tree)
        return {**result, "content": [*(result.get("content") or []), {"type": "text", "text": change}]} if change else result

    async def with_view(self, name: str, result: dict) -> dict:
        """A lookup that found nothing, or a page with regions its text can't describe, comes with
        the screen, once for each state of the page (and for those regions, once for each URL)."""
        if result.get("isError"):
            return result
        empty = name == "browser_find" and "No matches found" in _text(result)
        state = await self.page_state(False)
        regions = bool(state and state["opaque"])
        if not empty and not (regions and state["url"] not in self.seen_pages):
            return result
        why = ("It isn't in the page's text or controls." if empty else
               "This page has regions its text can't describe (a canvas, images, frames or a built-in player).")
        return await self.with_screen(result, why, state)

    async def with_screen(self, result: dict, why: str, state: dict | None) -> dict:
        """The result with the screen, unless the agent already has this state of the page."""
        key = (state["url"], state["dom"]) if state else None
        if key is not None and key == self.shown_state:  # the agent has this screen already
            return {**result, "content": [*(result.get("content") or []), {"type": "text", "text": SEE_IT.format(
                why=why).replace("this screenshot", "the last screenshot")}]}
        images = await self.screenshot()
        if not images:
            return result
        self.shown_state = key
        if state:
            self.seen_pages.add(state["url"])
        return {**result, "content": [*(result.get("content") or []),
                                      {"type": "text", "text": SEE_IT.format(why=why)}, *images]}

    # --- the task's own tab -----------------------------------------------------------------

    def _on_tab(self) -> bool:
        return (self.tab is not None and not self.tab.is_closed()
                and self.chrome.browser is not None and self.chrome.browser.is_connected())

    async def use_tab(self) -> str:
        """Before a tool runs: the session is on this task's tab, in front. An error rather than
        going on, because the session would otherwise act on whichever tab comes first."""
        try:
            async with asyncio.timeout(TAB_TIMEOUT):
                async with self.tab_lock:
                    if not self._on_tab():
                        await self.claim()
                    await self.chrome.cdp(self.tab, "Page.bringToFront")
            return ""
        except Exception as e:
            self.tab = None
            return (f"Couldn't get this task's browser tab ({type(e).__name__}). The browser may be "
                    "restarting; try again shortly.")

    async def claim(self) -> None:
        """Back to the tab this task used last, if it is still open; else a new one."""
        targets = await self.chrome.targets()
        self.known |= set(targets)
        for tid in reversed(care.tabs_of(self.store, self.owner)):
            if tid in targets and (page := await self.chrome.page_of(tid)) and await self.select(page):
                self.tab, self.tab_id = page, tid
                care.touch(self.store, self.owner, tid)
                self.touched = time.monotonic()
                return
        # A unique URL is what identifies the new tab in Chrome: the session only knows indexes.
        mark = f"about:blank#friday-{uuid.uuid4().hex}"
        r = await self.up.call("browser_tabs", {"action": "new", "url": mark}, self.passthrough, timeout=10)
        if r.get("isError"):
            raise RuntimeError("no new tab")
        for _ in range(30):
            targets = await self.chrome.targets()
            tid = next((t for t, info in targets.items() if info.get("url") == mark), "")
            page = await self.chrome.page_of(tid) if tid else None
            if page is not None:
                self.tab, self.tab_id = page, tid
                self.known |= set(targets)
                care.touch(self.store, self.owner, tid)
                self.touched = time.monotonic()
                return
            await asyncio.sleep(0.1)
        raise RuntimeError("new tab not found")

    async def listed(self) -> list[tuple[int, bool, str]]:
        """The session's tabs: (index, is it the current one, URL)."""
        r = await self.up.call("browser_tabs", {"action": "list"}, self.passthrough, timeout=8)
        return [(int(m[1]), bool(m[2]), m[3]) for m in TAB_LINE.finditer(_text(r))]

    async def select(self, page) -> bool:
        """Put the session on this page, whose index in the session's list is only known by URL."""
        same = [i for i, _, url in await self.listed() if url == page.url]
        for i in same:
            r = await self.up.call("browser_tabs", {"action": "select", "index": i}, self.passthrough, timeout=8)
            if not r.get("isError") and (len(same) == 1 or await self.current_page() is page):
                return True
        return False

    async def current_page(self):
        """The page the session is on. With several tabs on its URL only the page can tell: the
        session marks its own with a random value, and Friday's connection looks for it."""
        url = await self.current_url()
        pages = [p for p in await self.chrome.pages() if p.url == url]
        if len(pages) < 2:
            return pages[0] if pages else None
        mark = uuid.uuid4().hex
        await self.up.call("browser_evaluate", {"function": f"() => {{ window.__fridayTab = '{mark}'; }}"},
                           self.passthrough, timeout=8)
        for page in pages:
            try:
                if await page.evaluate("m => { const me = window.__fridayTab === m; "
                                       "if (me) delete window.__fridayTab; return me; }", mark):
                    return page
            except Exception:
                continue
        return None

    async def note_tabs(self, tabs_action: str | None, closed: bool = False) -> None:
        """After a tool: follow a change of tab, and add tabs this task opened to its lease (a
        login popup: its opener is ours, or it has none and nobody else claims it)."""
        try:
            async with asyncio.timeout(5):
                async with self.tab_lock:
                    if closed or tabs_action == "close":  # the session may be on a neighbour now
                        self.tab = None
                    elif tabs_action in ("new", "select"):
                        if (page := await self.current_page()) is not None:
                            self.tab, self.tab_id = page, await self.chrome.target_of(page)
                    targets = await self.chrome.targets()
                    mine = care.tabs_of(self.store, self.owner)
                    others = {t for k, x in care.leases(self.store).items() if k != care.LEASE + self.owner
                              for t in x["tabs"]}
                    new = [t for t, info in targets.items() if t not in self.known and t not in others
                           and (not info.get("openerId") or info["openerId"] in mine)]
                    self.known |= set(targets)
                    use = [self.tab_id] if self.tab is not None and self.tab_id not in others else []
                    stale = time.monotonic() - self.touched > LEASE_TOUCH
                    if stale or set(new) - set(mine) or (use and mine[-1:] != use):
                        care.touch(self.store, self.owner, *new, *use)
                        self.touched = time.monotonic()
        except Exception:  # bookkeeping: the tool's own result still stands
            pass

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

    async def frame_offset(self, page, args: dict) -> tuple[float, float] | None:
        """Where the frame holding this element starts on the page. The session marks the element's
        frame and Friday's connection looks for the mark, since refs only mean something there."""
        mark = uuid.uuid4().hex
        r = await self.up.call("browser_evaluate", {"function": f"(e) => {{ window.__fridayFrame = '{mark}'; return true; }}",
                                                    "element": args.get("element", ""), "target": args["target"]},
                               self.passthrough, timeout=8)
        if r.get("isError") or page is None:
            return None
        for frame in page.frames:
            try:
                if not await frame.evaluate("m => { const me = window.__fridayFrame === m; "
                                            "if (me) delete window.__fridayFrame; return me; }", mark):
                    continue
                element = await frame.frame_element()
                box = await element.bounding_box()
                inset = await element.evaluate("e => { const s = getComputedStyle(e); return "
                                               "[e.clientLeft + parseFloat(s.paddingLeft), e.clientTop + parseFloat(s.paddingTop)]; }")
                return (box["x"] + inset[0], box["y"] + inset[1]) if box else None
            except Exception:
                continue
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
            offset = await self.frame_offset(page, args)
            if offset:
                probe = {**probe, "x": probe["x"] + offset[0], "y": probe["y"] + offset[1]}
            else:
                remedy, why = "none", why + "; it is inside a frame whose position on the page couldn't be found"
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
        return next((url for _, current, url in await self.listed() if current), "")

    async def media(self) -> dict:
        """What the page's players are doing. Read twice, since only a moving position shows that
        one really plays. It changes nothing: players are operated through their own controls."""
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
                           "after a click, or draw on a canvas).")
        await asyncio.sleep(MEDIA_SAMPLE)
        out = []
        for frame, m in sorted(found, key=lambda fm: fm[1]["area"], reverse=True):
            try:
                after = (await frame.evaluate(MEDIA_STATE))[m["i"]]
            except Exception:  # the frame went away, or the player was replaced
                continue
            out.append({"tag": after["tag"], "state": media_note(m, after), "position": round(after["t"], 1),
                        "duration": round(after["d"], 1) if isfinite(after["d"] or 0) and after["d"] else None,
                        "rate": after["rate"], "muted": after["muted"], "buffered_to": round(after["buffered"], 1)})
        return _result(json.dumps(out, ensure_ascii=False))

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
        warm = asyncio.create_task(self.chrome.connect())  # connecting takes ~2s: do it before it's needed
        warm.add_done_callback(lambda t: t.exception())
        reader = asyncio.StreamReader(limit=1 << 25)
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, reader.feed_eof)
        try:
            while line := await reader.readline():
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                task = asyncio.create_task(self.handle(msg))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
        finally:
            for task in self.tasks:
                task.cancel()
            await self.up.close()


def main() -> None:
    asyncio.run(Relay(os.environ["FRIDAY_PLAYWRIGHT_URL"]).run())


if __name__ == "__main__":
    main()
