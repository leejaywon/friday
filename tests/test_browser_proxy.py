import asyncio
import json

from friday import browser_proxy as bp


class FakeUpstream:
    """The Playwright MCP server as browser_proxy uses it: tool calls in, results out."""

    def __init__(self, probe):
        self.calls, self.probe, self.shots = [], probe, iter(["before", "after", "after"])

    async def call(self, name, args, on_message, timeout=0):
        self.calls.append((name, args))
        if name == "browser_take_screenshot":
            return {"content": [{"type": "image", "data": next(self.shots), "mimeType": "image/jpeg"}]}
        if name == "browser_evaluate":
            return {"content": [{"type": "text", "text": f"### Result\n{json.dumps(self.probe)}\n\n### Ran Playwright code"}]}
        return {"content": [{"type": "text", "text": "clicked"}]}


def test_a_huge_page_is_cut_with_the_full_text_saved():
    page = "\n".join(f"- row {i}" for i in range(9000))
    result = {"content": [{"type": "text", "text": "[effect: changed]"}, {"type": "text", "text": page},
                          {"type": "image", "data": "x"}]}
    saved = []
    out = bp.bounded(result, lambda text: saved.append(text) or "/tmp/page.txt", limit=20_000)
    text = out["content"][1]["text"]
    assert sum(len(c.get("text", "")) for c in out["content"]) <= 20_000 and page in saved[0]
    assert "- row 0\n" in text and "- row 8999" in text and "/tmp/page.txt" in text
    assert out["content"][0]["text"] == "[effect: changed]" and out["content"][2]["type"] == "image"
    small = {"content": [{"type": "text", "text": "short"}]}
    assert bp.bounded(small, lambda text: 1 / 0) is small


def test_a_screenshot_reaches_the_agent_as_a_file_it_can_open():
    result = {"content": [{"type": "text", "text": "failed"}, {"type": "image", "data": "aGk=", "mimeType": "image/png"}],
              "isError": True}
    out = bp.images_to_files(result, lambda part: "/tmp/shot.png" if part["data"] == "aGk=" else "")
    assert [c["type"] for c in out["content"]] == ["text", "text"] and out["isError"]
    assert "/tmp/shot.png" in out["content"][1]["text"] and "view_image" in out["content"][1]["text"]
    plain = {"content": [{"type": "text", "text": "ok"}]}
    assert bp.images_to_files(plain, lambda part: 1 / 0) is plain


def test_a_click_on_a_moving_player_becomes_a_mouse_click_and_is_checked():
    relay = bp.Relay("http://127.0.0.1:1/mcp")
    moving = {"x": 640, "y": 360, "w": 1280, "h": 720, "vw": 1280, "vh": 900, "hidden": False,
              "disabled": False, "covered": False, "by": "", "framed": False}
    relay.up = FakeUpstream(moving)
    async def no_tab():
        return None  # no CDP here: the proxy falls back to the Playwright server's own tools
    relay.front = no_tab
    failed = {"content": [{"type": "text", "text": "TimeoutError: Timeout 5000ms exceeded.\n - locator resolved to "
                                                   "<div id=movie_player>\n - waiting for element to be visible, enabled and stable"}],
              "isError": True}
    out = asyncio.run(relay.fix_click("browser_click", {"target": "f1e5", "element": "player"}, failed))
    assert ("browser_mouse_click_xy", {"x": 640, "y": 360}) in relay.up.calls
    assert not out["isError"] and "page changed" in out["content"][0]["text"]

    # Covered, disabled, gone: the cause, and no blind clicking.
    assert bp.click_plan({**moving, "covered": True, "by": "div.consent"}, "")[0] == "escape"
    assert bp.click_plan({**moving, "disabled": True}, "")[0] == "none"
    assert bp.click_plan({**moving, "y": 2000}, "")[0] == "scroll"
    assert bp.click_plan(None, "waiting for locator('aria-ref=e9')")[0] == "refresh"


def test_media_status_reads_what_a_player_is_really_doing():
    start = {"t": 3, "paused": False, "ready": 4, "buffered": 9}
    assert bp.media_note(start, {**start, "t": 3.9}) == "playing"
    assert bp.media_note(start, {**start, "paused": True}) == "paused"
    assert "waiting for data" in bp.media_note(start, {**start, "ready": 1})
    assert "isn't supported" in bp.media_note(start, {**start, "error": [4, ""]})
    assert bp.MEDIA_TOOL["inputSchema"]["properties"] == {}  # it reads; it has nothing to operate


def test_an_action_lists_the_controls_it_made_appear_on_any_page():
    before = """- generic [ref=e1]:
  - button "Play" [ref=e2] [cursor=pointer]
  - button "0:05 of 2:00" [ref=e3]
  - link "Home" [ref=e4] [cursor=pointer]"""
    after = """- generic [ref=e1]:
  - button "Pause" [ref=e2] [cursor=pointer]
  - button "0:09 of 2:00" [ref=e3]
  - menu [ref=e7]:
    - menuitemradio "1.5x" [ref=e8]
    - generic [ref=e9] [cursor=pointer]: 1x
  - link "Home" [ref=e4] [cursor=pointer]"""
    change = bp.structure_change(before, after)
    assert 'button "Pause" [ref=e2]' in change and 'menuitemradio "1.5x" [ref=e8]' in change and 'generic "1x" [ref=e9]' in change
    assert "went away: button \"Play\"" in change
    assert "0:09" not in change and "Home" not in change  # a running clock isn't a new control
    assert bp.structure_change(before, before) == ""
