#!/usr/bin/env python3
"""Check audible continuity, not just successful decoding/STT, in the running browser.

Generates a 12-second tone, records it through the real PulseAudio/FFmpeg path and checks
for internal silence and a slow playback clock. No microphone, Groq or user content is used.
"""

import asyncio
from array import array
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from playwright.async_api import async_playwright

from friday import browser_audio as audio
from friday.config import load_config
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

DURATION = 12


def continuity(path: Path) -> dict:
    pcm = subprocess.check_output(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
                                   "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1"])
    samples = array("f", pcm)
    levels = [math.sqrt(sum(x*x for x in samples[i:i+320])/320)
              for i in range(0, len(samples)-319, 320)]
    audible = [i for i, rms in enumerate(levels) if rms > .01]
    if not audible:
        raise RuntimeError("No tone was captured.")
    middle = levels[audible[0]:audible[-1]+1]
    gaps = sum(level < .001 for level in middle)
    return dict(internal_silence_seconds=gaps*.02, internal_silence_fraction=gaps/len(middle),
                audible_span_seconds=len(middle)*.02)


async def check(origin: str) -> dict:
    cfg = load_config()
    latest = audio._state_dir(cfg)/"latest"
    previous = latest.read_text() if latest.exists() else ""
    record = None
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        for pg in browser.contexts[0].pages:
            for frame in pg.frames:
                if await frame.locator("video,audio").evaluate_all(
                        "els=>els.some(v=>!v.paused&&!v.muted&&v.volume>0)"):
                    raise RuntimeError("Another tab is audible. Pause it before the audio check.")
        page = await browser.contexts[0].new_page()
        try:
            await page.goto(origin)
            record = await asyncio.to_thread(audio.start, cfg, "Deployment continuity test", 1)
            await asyncio.sleep(2)
            await page.locator("button").click()
            started = time.monotonic()
            await page.wait_for_function("document.querySelector('audio').ended", timeout=30000)
            elapsed = time.monotonic()-started
            await asyncio.sleep(.5)  # drain the output buffer before closing the recording
            saved = await asyncio.to_thread(audio.stop, cfg, record["id"])
            metrics = await asyncio.to_thread(continuity, cfg.workspace/saved["path"])
            metrics["playback_wall_seconds"] = elapsed
            metrics["passed"] = (metrics["internal_silence_fraction"] < .005
                                 and abs(metrics["audible_span_seconds"]-DURATION) < .3
                                 and elapsed < DURATION+1)
            return metrics
        finally:
            await page.close()
            if record:
                await asyncio.to_thread(audio.stop, cfg, record["id"])
                # Only remove this generated diagnostic, leaving the user's latest recording.
                with audio._lock(cfg):
                    shutil.rmtree(audio._directory(cfg, record))
                    (audio._state_dir(cfg)/(record["id"]+".json")).unlink()
                    if latest.read_text().strip() == record["id"]:
                        if previous:
                            latest.write_text(previous)
                        else:
                            latest.unlink()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as folder:
        tone = Path(folder)/"tone.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                        f"sine=frequency=440:sample_rate=48000:duration={DURATION}", str(tone)], check=True)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/tone.wav":
                    data, mime = tone.read_bytes(), "audio/wav"
                else:
                    data = b'<button onclick="document.querySelector(\'audio\').play()">Play</button><audio src="/tone.wav"></audio>'
                    mime = "text/html"
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        Thread(target=server.serve_forever, daemon=True).start()
        try:
            result = asyncio.run(check(f"http://127.0.0.1:{server.server_port}"))
            print(json.dumps(result))
            raise SystemExit(0 if result["passed"] else 1)
        finally:
            server.shutdown()
            server.server_close()
