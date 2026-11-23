"""Chat turns in one long-lived `codex app-server` process instead of a `codex exec` per turn.

`codex exec resume` starts Codex, reloads the thread and reconnects the MCP servers on every
message (~6 s before the model even starts). The app-server keeps the chat's thread loaded, so a
short reply takes ~2 s. It speaks newline-delimited JSON-RPC on stdin/stdout: initialize, then
thread/start or thread/resume (with the same config codex_args passes as `-c`, see
agent.codex_overrides), then turn/start per message; the reply streams back as notifications.

A thread's MCP servers (and their HTTP headers) are fixed while it is loaded, and resuming a
loaded thread ignores new config. So when a turn needs a different config (the agent turned on a
toolset with use_tools, a local MCP service went down, mcp.toml changed), the thread is
unsubscribed, unloaded (thread_unload_delay_secs after that) and resumed with the new config.

Failures of the server itself (it can't start, it died, it broke the protocol) raise
CodexServerError, and the caller falls back to `codex exec` (agent.run_codex). Failures of the
turn (model errors, timeouts) come back in AgentResult.errors, as from run_codex.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

from .agent import DISABLED_FEATURES, AgentResult, codex_env, codex_overrides, light_config, mcp_endpoints, tool_args
from .config import Config

log = logging.getLogger(__name__)

UNLOAD_DELAY_SECONDS = 1  # how long the server keeps an unsubscribed thread loaded
UNLOAD_WAIT_SECONDS = 15  # waiting for that before giving up and restarting the server
REQUEST_TIMEOUT = 30
THREAD_TIMEOUT = 120  # thread/start and thread/resume connect the thread's MCP servers
INTERRUPT_WAIT_SECONDS = 15
VERSION_CHECK_SECONDS = 600  # how often `codex --version` is compared with the running server's
RETRY_START_AFTER = 600  # after the server failed to start, use `codex exec` for this long
ANSI = re.compile(r"\x1b\[[0-9;]*m")
# Items that aren't tool calls; any other item means the turn has started doing things.
NOT_TOOLS = {"userMessage", "agentMessage", "reasoning", "plan", "hookPrompt", "contextCompaction"}
STOP = {"method": "friday/stop"}  # put in a running turn's queue by stop_turn (the [Stop] button)
SERVER_NAMES = {"playwright": "browser"}


def step_label(item: dict) -> str:
    """What a tool item is doing, for the turn's status line: "browser: click", "running a command"."""
    kind = item.get("type")
    if kind == "mcpToolCall":
        server = str(item.get("server") or "")
        tool = str(item.get("tool") or "").removeprefix("browser_").replace("_", " ")
        args = item.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        # What it acts on: the page, the element, the text it looks for.
        what = next((str(args[k]) for k in ("url", "element", "text", "key", "action", "query", "name")
                     if isinstance(args, dict) and args.get(k)), "")
        what = " ".join(re.sub(r"^https?://(www\.)?", "", what).split())
        return f"{SERVER_NAMES.get(server, server)}: {tool}" + (f" «{what[:50]}»" if what else "")
    if kind == "commandExecution":
        return "running a command"
    if kind == "webSearch" or (str(kind).lower() == "extension" and item.get("kind") == "web.search"):
        action = item.get("action") if isinstance(item.get("action"), dict) else {}
        query = item.get("query") or action.get("query") or " / ".join(action.get("queries") or [])
        query = " ".join(str(query).split())
        return "searching the web" + (f" «{query[:50]}»" if query else "")
    if kind == "dynamicToolCall":
        return str(item.get("tool") or "a tool").replace("_", " ")
    return str(kind or "")


def item_error(item: dict) -> str:
    """Why a finished tool item failed ("" when it didn't): its error, or an MCP result marked isError."""
    if item.get("status") == "failed" or item.get("error"):
        err = item.get("error")
        return str((err or {}).get("message") if isinstance(err, dict) else err or "failed")[:500]
    result = item.get("result")
    if isinstance(result, dict) and result.get("isError"):
        return " ".join(str(c.get("text", "")) for c in result.get("content") or []
                        if isinstance(c, dict))[:500] or "failed"
    return ""


class CodexServerError(Exception):
    """The app-server failed, not the turn. partial: the failed turn had already called tools."""

    def __init__(self, message: str, partial: bool = False):
        super().__init__(message)
        self.partial = partial


def _version(text: str) -> str:
    m = re.search(r"\d+\.\d+\.\d+\S*", text or "")
    return m.group(0) if m else ""


class CodexServer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.proc: asyncio.subprocess.Process | None = None
        self.version = ""  # what the running server reported in initialize
        self._env: tuple = ()  # what the running server was started with (TZ)
        self._reader: asyncio.Task | None = None
        self._err_reader: asyncio.Task | None = None
        self._stderr: deque[str] = deque(maxlen=20)
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._turns: dict[str, asyncio.Queue] = {}  # thread id -> notifications for its running turn
        self._closed: dict[str, asyncio.Event] = {}  # thread id -> set by thread/closed
        self._loaded: dict[str, dict] = {}  # thread id -> the config it was loaded with
        # Starting the server and opening threads, one at a time. Turns on different threads run
        # at the same time (the bot's chat lanes); a thread has one turn at a time.
        self._lock = asyncio.Lock()
        self._keys: dict[str, str] = {}  # the caller's key for a running turn (a chat lane) -> thread id
        self._turn_ids: dict[str, str] = {}  # thread id -> its running turn's id (steer)
        self._checked_at = 0.0
        self._broken_until = 0.0

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None and bool(self._reader) and not self._reader.done()

    # --- process --------------------------------------------------------------

    async def _start(self, tz: str) -> None:
        args = [self.cfg.codex_bin, "app-server", *DISABLED_FEATURES,
                "-c", f"thread_unload_delay_secs={UNLOAD_DELAY_SECONDS}"]
        self._stderr.clear()
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *args, cwd=self.cfg.workspace, env=codex_env(self.cfg, tz),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=1 << 24,  # one JSON line can hold a whole tool result
                start_new_session=True,  # its own process group: stop() also ends its MCP servers
            )
        except OSError as e:
            raise CodexServerError(f"can't start codex app-server: {e}") from e
        self._env = (tz, self.cfg.codex_bin)
        self._reader = asyncio.create_task(self._read(self.proc), name="codex-app-server")
        self._err_reader = asyncio.create_task(self._read_stderr(self.proc), name="codex-app-server-stderr")
        try:
            init = await self._request("initialize", {
                "clientInfo": {"name": "friday", "title": "Friday", "version": "1"},
                "capabilities": {"experimentalApi": True}})
        except CodexServerError:
            await self._stop()
            raise
        self._notify("initialized")
        self.version = _version(init.get("userAgent", ""))
        self._checked_at = time.monotonic()
        log.info("codex app-server started (pid %s, %s)", self.proc.pid, init.get("userAgent"))

    async def _stop(self) -> None:
        proc, self.proc = self.proc, None
        self._loaded.clear()
        if proc and proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                await proc.wait()
        for t in (self._reader, self._err_reader):
            if t:
                t.cancel()
        self._fail_all(CodexServerError("codex app-server stopped"))

    async def close(self) -> None:
        """Stop the server (Friday is shutting down)."""
        await self._stop()

    async def installed_version(self) -> str:
        proc = await asyncio.create_subprocess_exec(
            self.cfg.codex_bin, "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 20)
        return _version(out.decode(errors="replace"))

    async def _ensure(self, tz: str) -> None:
        """A running server for this turn: restarted if it died, the timezone changed or Codex
        was upgraded since it started."""
        if self.running:
            restart = ""
            if self._turns:  # not while other turns run: restarting would end them
                return
            if self._env != (tz, self.cfg.codex_bin):
                restart = "settings changed"
            elif time.monotonic() - self._checked_at > VERSION_CHECK_SECONDS:
                self._checked_at = time.monotonic()
                try:
                    installed = await self.installed_version()
                except (OSError, TimeoutError) as e:
                    installed = ""
                    log.warning("codex --version failed: %s", e)
                if installed and installed != self.version:
                    restart = f"codex {self.version} -> {installed}"
            if not restart:
                return
            log.info("restarting codex app-server: %s", restart)
            await self._stop()
        elif self.proc:
            await self._stop()  # it died: clean up before starting again
        if time.monotonic() < self._broken_until:
            raise CodexServerError("codex app-server failed to start recently")
        try:
            await self._start(tz)
        except CodexServerError:
            self._broken_until = time.monotonic() + RETRY_START_AFTER
            raise

    # --- JSON-RPC -----------------------------------------------------------------

    def _send(self, obj: dict) -> None:
        if not self.running:
            raise CodexServerError("codex app-server is not running")
        self.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())  # type: ignore[union-attr]

    def _notify(self, method: str, params: dict | None = None) -> None:
        self._send({"method": method, **({"params": params} if params is not None else {})})

    async def _request(self, method: str, params: dict, timeout: float = REQUEST_TIMEOUT) -> dict:
        self._next_id += 1
        rid = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            self._send({"id": rid, "method": method, "params": params})
            msg = await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            raise CodexServerError(f"codex app-server didn't answer {method} within {timeout}s") from None
        finally:
            self._pending.pop(rid, None)
        if "error" in msg:
            raise CodexServerError(f"{method}: {msg['error'].get('message') or msg['error']}")
        return msg.get("result") or {}

    async def _read(self, proc: asyncio.subprocess.Process) -> None:
        try:
            while line := await proc.stdout.readline():  # type: ignore[union-attr]
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("codex app-server: %s", line[:200])
                    continue
                if "method" in msg and "id" in msg:
                    self._answer(msg)
                elif "id" in msg:
                    fut = self._pending.get(msg["id"])
                    if fut and not fut.done():
                        fut.set_result(msg)
                else:
                    self._route(msg)
        except (ValueError, OSError) as e:  # e.g. a line over the limit
            log.warning("codex app-server output unreadable: %s", e)
        try:
            code = await asyncio.wait_for(proc.wait(), 2)
        except TimeoutError:
            code = "output closed"  # _ensure stops it before the next turn
        tail = " | ".join(list(self._stderr)[-2:])
        if self.proc is proc:
            log.warning("codex app-server exited (%s): %s", code, tail)
            self._loaded.clear()
            self._fail_all(CodexServerError(f"codex app-server exited ({code})" + (f": {tail}" if tail else "")))

    async def _read_stderr(self, proc: asyncio.subprocess.Process) -> None:
        while line := await proc.stderr.readline():  # type: ignore[union-attr]
            text = ANSI.sub("", line.decode(errors="replace")).rstrip()
            if text:
                self._stderr.append(text[:300])
                log.debug("codex app-server stderr: %s", text)

    def _fail_all(self, err: CodexServerError) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(err)
        for q in self._turns.values():
            q.put_nowait(err)

    def _answer(self, msg: dict) -> None:
        """Requests from the server. With approval_policy="never" none should ask for approval;
        anything that does is declined, as nobody is at a terminal to answer."""
        method = msg["method"]
        if method == "currentTime/read":
            reply = {"id": msg["id"], "result": {"currentTimeAt": int(time.time())}}
        else:
            log.warning("codex app-server asked %s; declined", method)
            reply = {"id": msg["id"], "error": {"code": -32601, "message": "not supported by Friday"}}
        try:
            self._send(reply)
        except CodexServerError:
            pass

    def _route(self, msg: dict) -> None:
        params = msg.get("params") or {}
        tid = params.get("threadId")
        if msg.get("method") == "thread/closed" and tid:
            self._loaded.pop(tid, None)
            if ev := self._closed.get(tid):
                ev.set()
        if tid and (q := self._turns.get(tid)):
            q.put_nowait(msg)

    # --- threads ------------------------------------------------------------------

    async def _unload(self, tid: str, wait: bool) -> None:
        """Unsubscribe from a thread; with wait, until the server has unloaded it."""
        ev = self._closed[tid] = asyncio.Event()
        try:
            r = await self._request("thread/unsubscribe", {"threadId": tid})
            self._loaded.pop(tid, None)
            if wait and r.get("status") == "unsubscribed":
                try:
                    await asyncio.wait_for(ev.wait(), UNLOAD_WAIT_SECONDS)
                except TimeoutError:
                    # Resuming it now would keep the old config: start over instead.
                    log.warning("codex app-server didn't unload thread %s; restarting it", tid)
                    tz = self._env[0]
                    await self._stop()
                    await self._start(tz)
        finally:
            self._closed.pop(tid, None)

    async def _open(self, thread_id: str | None, config: dict, model: str, cwd: Path | None = None) -> str:
        """The thread to run the turn in, loaded with this config."""
        if thread_id and thread_id in self._loaded:
            if self._loaded[thread_id] == config:
                return thread_id
            log.info("reloading thread %s with new config", thread_id)
            await self._unload(thread_id, wait=True)
        # Idle threads are unloaded (they hold MCP connections); not ones with a turn running.
        for other in [t for t in self._loaded if t != thread_id and t not in self._turns]:
            await self._unload(other, wait=False)
        params: dict = {"cwd": str(cwd or self.cfg.workspace), "config": config}
        if model:
            params["model"] = model
        if thread_id:
            r = await self._request("thread/resume", {**params, "threadId": thread_id, "excludeTurns": True},
                                    THREAD_TIMEOUT)
        else:
            r = await self._request("thread/start", {**params, "ephemeral": False}, THREAD_TIMEOUT)
        tid = (r.get("thread") or {}).get("id") or thread_id
        if not tid:
            raise CodexServerError("codex app-server returned no thread id")
        self._loaded[tid] = config
        return tid

    # --- turns --------------------------------------------------------------------

    async def run(
        self,
        prompt: str,
        thread_id: str | None = None,
        images: list[Path] | None = None,
        model: str = "",
        effort: str = "",
        toolsets: frozenset[str] | set[str] = frozenset(),
        tz: str = "",
        chat: bool | str = True,
        on_note: Callable[[str], None] | None = None,
        on_step: Callable[[str, str], None] | None = None,
        key: str = "",
        job_id: int | None = None,
        workdir: Path | None = None,
        timeout: float = 0,
    ) -> AgentResult:
        """One turn, like agent.run_codex. Turns on different threads run at the same time.
        on_note gets the agent's progress lines ("checking X") as they come; on_step(item id,
        step_label) each tool call it starts and on_step(item id, "") when it ends. key names the
        turn for stop_turn. job_id, workdir, timeout: a delegated job's worker, which runs in its
        own folder for longer than a chat turn. Raises CodexServerError when the server fails."""
        async with self._lock:
            try:
                await self._ensure(tz or self.cfg.default_tz)
                config = codex_overrides(self.cfg, toolsets=toolsets, chat=False if job_id else chat,
                                         http=mcp_endpoints(toolsets), job_id=job_id, workdir=workdir)
                model = model or self.cfg.codex_model
                tid = await self._open(thread_id, config, model, workdir)
            except CodexServerError:
                await self._recover(thread_id)
                raise
        try:
            return await self._turn(tid, prompt, images or [], model, effort or self.cfg.codex_effort, on_note,
                                    on_step, key, timeout)
        except CodexServerError:
            async with self._lock:
                await self._recover(tid)
            raise

    async def light(self, prompt: str, model: str = "", effort: str = "", timeout: float = 45) -> str:
        """A short answer from a small model in a throwaway thread with no tools (progress updates,
        agent.light_reply); "" when it fails. Runs alongside the chat's turns."""
        tid = ""
        try:
            async with self._lock:
                await self._ensure(self._env[0] if self.running else self.cfg.default_tz)
                params: dict = {"cwd": str(self.cfg.workspace), "config": light_config(self.cfg), "ephemeral": True}
                if model:
                    params["model"] = model
                r = await self._request("thread/start", params, THREAD_TIMEOUT)
                tid = (r.get("thread") or {}).get("id") or ""
            if not tid:
                return ""
            res = await asyncio.wait_for(self._turn(tid, prompt, [], model, effort), timeout)
            return res.text if res.ok else ""
        except (CodexServerError, TimeoutError) as e:
            log.info("light reply failed: %s", e)
            return ""
        finally:
            if tid and self.running:
                try:
                    async with self._lock:
                        await self._unload(tid, wait=False)
                except CodexServerError:
                    pass

    async def _recover(self, tid: str | None) -> None:
        """After a server failure: the caller retries with `codex exec`, which writes to the thread
        on disk, so the next turn here must reload it rather than use a stale copy. The server is
        restarted, unless other turns are running on it: then only this thread is unloaded."""
        if not self._turns or not self.running:
            await self._stop()
            return
        if tid:
            self._loaded.pop(tid, None)
            try:
                await self._request("thread/unsubscribe", {"threadId": tid})
            except CodexServerError as e:
                log.info("couldn't unload thread %s: %s", tid, e)

    async def _turn(self, tid: str, prompt: str, images: list[Path], model: str, effort: str,
                    on_note: Callable[[str], None] | None = None,
                    on_step: Callable[[str, str], None] | None = None, key: str = "",
                    timeout: float = 0) -> AgentResult:
        limit = timeout or self.cfg.codex_timeout_seconds
        q: asyncio.Queue = asyncio.Queue()
        self._turns[tid] = q
        if key:
            self._keys[key] = tid
        messages: list[str] = []
        errors: list[str] = []
        servers: set[str] = set()
        calls: list[tuple[str, dict]] = []
        tokens, partial, turn_id = 0, False, ""
        try:
            params: dict = {"threadId": tid, "input": [{"type": "localImage", "path": str(p)} for p in images]
                            + [{"type": "text", "text": prompt, "text_elements": []}]}
            if model:
                params["model"] = model
            if effort:
                params["effort"] = effort
            turn_id = (await self._request("turn/start", params)).get("turn", {}).get("id", "")
            self._turn_ids[tid] = turn_id
            deadline = time.monotonic() + limit
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), max(0.0, deadline - time.monotonic()))
                except TimeoutError:
                    await self._interrupt(tid, turn_id, q)
                    return AgentResult("", tid, [f"Codex took longer than {limit:.0f}s and was stopped."],
                                       servers, tokens, calls)
                if isinstance(msg, CodexServerError):
                    raise CodexServerError(str(msg), partial)
                if msg is STOP:
                    await self._interrupt(tid, turn_id, q)
                    return AgentResult("", tid, ["stopped"], servers, tokens, calls)
                method, p = msg.get("method"), msg.get("params") or {}
                if p.get("turnId") not in (None, turn_id) and method != "turn/completed":
                    continue
                item = p.get("item") or {}
                tool = bool(item) and item.get("type") not in NOT_TOOLS
                if tool and method == "item/started":
                    partial = True
                    if on_step:
                        on_step(str(item.get("id")), step_label(item))
                elif tool and method == "item/completed":
                    if on_step:
                        failed = item_error(item)
                        on_step(str(item.get("id")), "", failed) if failed else on_step(str(item.get("id")), "")
                    if item.get("type") == "mcpToolCall":
                        servers.add(item.get("server") or "")
                        if item.get("server") == "friday":
                            calls.append((str(item.get("tool") or ""), tool_args(item.get("arguments"))))
                elif method == "item/completed" and item.get("type") == "agentMessage":
                    messages.append(item.get("text", ""))
                    if on_note and item.get("phase") == "commentary":
                        on_note(item.get("text", ""))
                elif method == "thread/tokenUsage/updated":
                    # The last model request's input: the context size (as agent.last_request_tokens).
                    tokens = int(((p.get("tokenUsage") or {}).get("last") or {}).get("inputTokens") or tokens)
                elif method == "error" and not p.get("willRetry"):
                    errors.append(str((p.get("error") or {}).get("message") or "error"))
                elif method == "turn/completed":
                    turn = p.get("turn") or {}
                    if turn.get("id") not in (None, "", turn_id):
                        continue
                    status = turn.get("status")
                    err = str((turn.get("error") or {}).get("message") or "")
                    if status == "failed" and err not in errors:
                        errors.append(err or "turn failed")
                    elif status == "interrupted":
                        errors.append(err or "the turn was interrupted")
                    break
        except asyncio.CancelledError:  # e.g. Friday is stopping: don't leave the turn running
            if turn_id:
                try:
                    self._send({"id": 0, "method": "turn/interrupt", "params": {"threadId": tid, "turnId": turn_id}})
                except CodexServerError:
                    pass
            raise
        finally:
            self._turns.pop(tid, None)
            self._turn_ids.pop(tid, None)
            if key and self._keys.get(key) == tid:
                del self._keys[key]
        if errors:
            log.warning("codex errors: %s", errors)
        return AgentResult((messages[-1] if messages else "").strip(), tid, errors, servers, tokens, calls)

    async def steer(self, key: str, text: str, images: list[Path] | None = None) -> bool:
        """Add the user's new message to the turn run() was given this key for, while it runs
        (turn/steer). False when that turn isn't running (any more)."""
        tid = self._keys.get(key, "")
        turn_id = self._turn_ids.get(tid, "")
        if not turn_id:
            return False
        try:
            await self._request("turn/steer", {
                "threadId": tid, "expectedTurnId": turn_id,
                "input": [{"type": "localImage", "path": str(p)} for p in images or []]
                + [{"type": "text", "text": text, "text_elements": []}]})
            return True
        except CodexServerError as e:
            log.info("steer failed: %s", e)
            return False

    def stop_turn(self, key: str) -> bool:
        """Stop the turn run() was given this key for (the [Stop] button): it's interrupted and
        returns. False when it hasn't started yet."""
        q = self._turns.get(self._keys.get(key, ""))
        if q:
            q.put_nowait(STOP)
        return q is not None

    async def _interrupt(self, tid: str, turn_id: str, q: asyncio.Queue) -> None:
        """Stop a turn that ran too long; restart the server if it doesn't stop."""
        try:
            await self._request("turn/interrupt", {"threadId": tid, "turnId": turn_id})
            end = time.monotonic() + INTERRUPT_WAIT_SECONDS
            while True:
                msg = await asyncio.wait_for(q.get(), max(0.0, end - time.monotonic()))
                if isinstance(msg, CodexServerError) or msg.get("method") == "turn/completed":
                    return
        except (CodexServerError, TimeoutError):
            log.warning("codex app-server didn't stop turn %s; restarting it", turn_id)
            await self._stop()
