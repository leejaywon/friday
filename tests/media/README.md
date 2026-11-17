# Browser playback fixtures

Five seconds of a generated 160x90 test pattern (H.264 Baseline, 15 fps) and/or a
440 Hz sine tone (AAC-LC). These contain no third-party content. Run
`.venv/bin/python deploy/check-browser.py` on the server to verify actual video
frames and nonzero decoded audio samples through Web Audio, using the existing
browser over CDP. This checks decoding, not an attached speaker or audio capture.

Generated with FFmpeg:

```sh
ffmpeg -f lavfi -i testsrc2=size=160x90:rate=15 \
  -f lavfi -i sine=frequency=440:sample_rate=44100 -t 5 \
  -c:v libx264 -profile:v baseline -pix_fmt yuv420p -preset veryslow -crf 32 \
  -c:a aac -b:a 48k -movflags +faststart h264-aac.mp4
ffmpeg -i h264-aac.mp4 -an -c:v copy -movflags +faststart h264.mp4
ffmpeg -i h264-aac.mp4 -vn -c:a copy -movflags +faststart aac.m4a
```
