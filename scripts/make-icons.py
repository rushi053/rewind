#!/usr/bin/env python3
"""Render assets/icon.svg into the PNG/ICNS/ICO files shipped in src/rewind_cli/static/.

Development-only: needs Playwright with Chromium (`pip install playwright && playwright install chromium`).
The ICNS and ICO containers are written by hand (both just wrap PNG data), so no
macOS or Windows tools are required.
"""
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SVG = ROOT / "assets" / "icon.svg"
OUT = ROOT / "src" / "rewind_cli" / "static"
SIZES = [16, 32, 48, 64, 128, 256, 512, 1024]

# ICNS element types that may hold PNG data, by pixel size
ICNS_TYPES = [("icp4", 16), ("icp5", 32), ("icp6", 64), ("ic07", 128), ("ic08", 256), ("ic09", 512),
              ("ic11", 32), ("ic12", 64), ("ic13", 256), ("ic14", 512)]


def render():
    from playwright.sync_api import sync_playwright

    svg = SVG.read_text()
    pngs = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for size in SIZES:
            page = browser.new_page(viewport={"width": size, "height": size})
            sized = svg.replace('width="1024" height="1024"', 'width="%d" height="%d"' % (size, size), 1)
            page.set_content('<html><body style="margin:0;background:transparent">%s</body></html>' % sized)
            pngs[size] = page.screenshot(omit_background=True)
        browser.close()
    return pngs


def icns(pngs):
    body = b""
    for ostype, size in ICNS_TYPES:
        data = pngs[size]
        body += ostype.encode("ascii") + struct.pack(">I", len(data) + 8) + data
    return b"icns" + struct.pack(">I", len(body) + 8) + body


def ico(pngs, sizes=(16, 32, 48, 64, 128, 256)):
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries, blobs = b"", b""
    for s in sizes:
        data = pngs[s]
        dim = 0 if s >= 256 else s
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    return header + entries + blobs


def main():
    pngs = render()
    (OUT / "icon.icns").write_bytes(icns(pngs))
    (OUT / "icon.ico").write_bytes(ico(pngs))
    (OUT / "icon-256.png").write_bytes(pngs[256])
    (OUT / "icon-32.png").write_bytes(pngs[32])
    for name in ("icon.icns", "icon.ico", "icon-256.png", "icon-32.png"):
        print("wrote", OUT / name, (OUT / name).stat().st_size, "bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
