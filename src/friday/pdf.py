"""Read a PDF from a worker's shell: `python -m friday.pdf FILE [--pages 2-5]`.

Prints the text page by page. A page with next to no text is usually a scan, so it is also saved
as a PNG in ./pdf-pages for the agent to look at. Output is bounded: it stops at a page boundary
and says which --pages to ask for next.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pypdfium2 as pdfium

MAX_CHARS = 40_000  # of text per call
MAX_IMAGES = 6  # scanned pages rendered per call
THIN_PAGE = 20  # fewer characters than this (a stamped page number) isn't a text layer
SCALE = 2  # 144 dpi: small print stays readable


def page_range(spec: str, total: int) -> range:
    """'3' or '2-5' or '4-' as 0-based page indexes, within the document."""
    first, dash, last = spec.strip().partition("-")
    start = int(first) if first else 1
    end = (int(last) if last else total) if dash else start
    if start < 1 or end < start or start > total:
        raise ValueError(f"no pages {spec!r}: the document has {total}")
    return range(start - 1, min(end, total))


def read(path: Path, pages: str = "", out_dir: Path = Path("pdf-pages")) -> str:
    doc = pdfium.PdfDocument(path)
    try:
        total = len(doc)
        wanted = page_range(pages or "1-", total) if total else range(0)
        out, chars, images = [f"{path.name}: {total} pages"], 0, 0
        for i in wanted:
            page = doc[i]
            textpage = page.get_textpage()
            text = textpage.get_text_bounded().replace("\r\n", "\n").strip()
            textpage.close()
            thin = len("".join(text.split())) < THIN_PAGE
            if (chars and chars + len(text) > MAX_CHARS) or (thin and images >= MAX_IMAGES):
                out.append(f"[Stopped here to keep this short. Continue with --pages {i + 1}-{wanted[-1] + 1}]")
                break
            head = f"--- page {i + 1} ---"
            if thin:
                out_dir.mkdir(parents=True, exist_ok=True)
                image = (out_dir / f"{path.stem}-p{i + 1}.png").resolve()
                page.render(scale=SCALE).to_pil().save(image)
                images += 1
                head = f"--- page {i + 1} (no text layer, likely a scan; look at the image: {image}) ---"
            out += [head, text] if text else [head]
            chars += len(text)
            page.close()
        return "\n".join(out)
    finally:
        doc.close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="friday.pdf", description="Print a PDF's text.")
    parser.add_argument("file", type=Path)
    parser.add_argument("--pages", default="", help="e.g. 3, 2-5 or 4-")
    args = parser.parse_args()
    try:
        print(read(args.file, args.pages))
    except (pdfium.PdfiumError, OSError, ValueError) as e:
        print(f"Can't read {args.file}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
