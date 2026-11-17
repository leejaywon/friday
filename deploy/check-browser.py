#!/usr/bin/env python3
"""Check real H.264/AAC decoding in Friday's running browser; never reads login data.

Run from the checkout: .venv/bin/python deploy/check-browser.py
The fixtures are generated test patterns and a 440 Hz tone (see tests/media/README.md).
"""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread

from playwright.async_api import async_playwright

MEDIA = Path(__file__).resolve().parent.parent / "tests/media"
CASES = {"h264.mp4": (True, False), "aac.m4a": (False, True), "h264-aac.mp4": (True, True)}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        name = self.path.lstrip("/")
        if self.path == "/":
            content, mime = b'<button id="play">Check playback</button>', "text/html"
        elif name in CASES:
            content = (MEDIA / name).read_bytes()
            mime = "audio/mp4" if name.endswith("m4a") else "video/mp4"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *args):
        pass


PLAY = """({name, video, audio}) => {
  document.querySelector('#play').onclick = () => {
    window.playbackResult = (async () => {
      const media = document.createElement(video ? 'video' : 'audio');
      document.body.append(media);
      media.src = '/' + name;
      let ctx, analyser;
      if (audio) {
        ctx = new AudioContext();
        await ctx.resume();
        analyser = ctx.createAnalyser();
        ctx.createMediaElementSource(media).connect(analyser);
        analyser.connect(ctx.destination);
      }
      await media.play();
      let peak = 0;
      const start = performance.now();
      while (performance.now() - start < 2200) {
        if (analyser) {
          const samples = new Float32Array(analyser.fftSize);
          analyser.getFloatTimeDomainData(samples);
          peak = Math.max(peak, ...samples.map(Math.abs));
        }
        await new Promise(resolve => setTimeout(resolve, 50));
      }
      const result = {
        file: name, currentTime: media.currentTime, duration: media.duration,
        frames: video ? media.getVideoPlaybackQuality().totalVideoFrames : null,
        audioPeak: audio ? peak : null, audioContext: ctx?.state ?? null,
        error: media.error?.message ?? null
      };
      media.pause();
      if (ctx) await ctx.close();
      media.remove();
      return result;
    })();
  };
}"""


async def check(origin):
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        page = await browser.contexts[0].new_page()
        passed = True
        try:
            print(json.dumps({"browser": browser.version}))
            for name, (video, audio) in CASES.items():
                await page.goto(origin)
                await page.evaluate(PLAY, {"name": name, "video": video, "audio": audio})
                await page.locator("#play").click()
                result = await asyncio.wait_for(page.evaluate("window.playbackResult"), timeout=20)
                result["passed"] = (
                    result["error"] is None and result["currentTime"] > 1
                    and (not video or result["frames"] > 0)
                    and (not audio or result["audioPeak"] > 0.001)
                )
                passed &= result["passed"]
                print(json.dumps(result))
        finally:
            await page.close()
        return passed


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        raise SystemExit(0 if asyncio.run(check(f"http://127.0.0.1:{server.server_port}")) else 1)
    finally:
        server.shutdown()
        server.server_close()
