"""A stand-in for `codex app-server` (and `codex --version`) for test_codex_server.py.

Logs every request to $FAKE_CODEX_LOG. A turn's text decides what happens: CRASH exits mid-turn,
HANG never finishes (until turn/interrupt), FAIL fails like a usage limit; anything else replies
"reply: <text> (<n> images)" after a Friday tool call.
"""

import json
import os
import sys

if sys.argv[1:] == ["--version"]:
    print("codex-cli 0.0.1")
    sys.exit()

log = open(os.environ["FAKE_CODEX_LOG"], "a")
threads = 0


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def note(method, **params):
    send({"method": method, "params": params})


for line in sys.stdin:
    msg = json.loads(line)
    method, p, rid = msg.get("method"), msg.get("params") or {}, msg.get("id")
    log.write(json.dumps({"pid": os.getpid(), "method": method, "params": p}) + "\n")
    log.flush()
    if rid is None or method is None:
        continue
    if method == "initialize":
        send({"id": rid, "result": {"userAgent": "friday/0.0.1 (test)"}})
    elif method == "thread/start":
        threads += 1
        send({"id": rid, "result": {"thread": {"id": f"t{threads}"}}})
    elif method == "thread/resume":
        send({"id": rid, "result": {"thread": {"id": p["threadId"]}}})
    elif method == "thread/unsubscribe":
        send({"id": rid, "result": {"status": "unsubscribed"}})
        note("thread/closed", threadId=p["threadId"])
    elif method == "turn/interrupt":
        send({"id": rid, "result": {}})
        note("turn/completed", threadId=p["threadId"], turn={"id": p["turnId"], "status": "interrupted", "items": []})
    elif method == "turn/start":
        tid, turn = p["threadId"], "turn-1"
        text = next(i["text"] for i in p["input"] if i["type"] == "text")
        images = sum(i["type"] == "localImage" for i in p["input"])
        send({"id": rid, "result": {"turn": {"id": turn, "status": "inProgress", "items": []}}})
        if "CRASH" in text:
            note("item/started", threadId=tid, turnId=turn, item={"type": "mcpToolCall", "id": "i1", "server": "friday"})
            sys.exit(1)
        if "HANG" in text:
            note("item/started", threadId=tid, turnId=turn,
                 item={"type": "mcpToolCall", "id": "i1", "server": "playwright", "tool": "browser_click"})
            continue
        if "FAIL" in text:
            note("error", threadId=tid, turnId=turn, willRetry=False, error={"message": "usage limit reached"})
            note("turn/completed", threadId=tid, turn={"id": turn, "status": "failed", "items": [],
                                                       "error": {"message": "usage limit reached"}})
            continue
        note("item/completed", threadId=tid, turnId=turn, item={"type": "mcpToolCall", "id": "i1", "server": "friday"})
        note("item/completed", threadId=tid, turnId=turn, item={"type": "agentMessage", "id": "i2", "text": "draft"})
        note("item/completed", threadId=tid, turnId=turn,
             item={"type": "agentMessage", "id": "i3", "text": f"reply: {text} ({images} images)"})
        note("thread/tokenUsage/updated", threadId=tid, turnId=turn,
             tokenUsage={"last": {"inputTokens": 1234}, "total": {"inputTokens": 5000}})
        note("turn/completed", threadId=tid, turn={"id": turn, "status": "completed", "items": []})
    else:
        send({"id": rid, "error": {"code": -32601, "message": "unknown"}})
