#!/usr/bin/env python3
"""Render ANSI-colored terminal output (stdin) as a terminal-window SVG (stdout).

Used to produce docs/log.svg from *real* `rewind log` output:

    REWIND_COLOR=always COLUMNS=100 rewind log | python scripts/ansi2svg.py --title "rewind log" > docs/log.svg
"""

import argparse
import re
import sys
from xml.sax.saxutils import escape

PALETTE = {
    30: "#4b5263", 31: "#f0646a", 32: "#4fc88f", 33: "#e2b85c", 34: "#7aa2f7", 35: "#c792ea", 36: "#5cc5d6", 37: "#d7dae0",
    90: "#6b717c", 91: "#ff7b80", 92: "#6fe0a8", 93: "#f0cc7a", 94: "#9bb8ff", 95: "#dcaaf5", 96: "#7fdcea", 97: "#ffffff",
}
FG = "#d7dae0"
BG = "#0d0e10"
SGR = re.compile(r"\x1b\[([0-9;]*)m")


def parse(line):
    """Yield (text, style dict) runs."""
    style = {}
    pos = 0
    for m in SGR.finditer(line):
        if m.start() > pos:
            yield line[pos:m.start()], dict(style)
        codes = [int(c) for c in m.group(1).split(";") if c] or [0]
        for c in codes:
            if c == 0:
                style = {}
            elif c == 1:
                style["bold"] = True
            elif c == 2:
                style["dim"] = True
            elif c == 3:
                style["italic"] = True
            elif c in PALETTE:
                style["fg"] = PALETTE[c]
            elif c == 39:
                style.pop("fg", None)
        pos = m.end()
    if pos < len(line):
        yield line[pos:], dict(style)


def render(text, title, cols=None, font_size=13.5):
    lines = text.rstrip("\n").split("\n")
    char_w = font_size * 0.6
    line_h = font_size * 1.55
    width_chars = max([cols or 0] + [len(SGR.sub("", l)) for l in lines])
    pad_x, pad_top, pad_bottom = 22, 52, 22
    width = int(pad_x * 2 + width_chars * char_w)
    height = int(pad_top + len(lines) * line_h + pad_bottom)
    out = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" role="img" aria-label="%s">'
        % (width, height, width, height, escape(title)),
        "<style>text{font-family:ui-monospace,'SF Mono',SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace;font-size:%spx;white-space:pre}</style>" % font_size,
        '<rect width="100%%" height="100%%" rx="10" fill="%s"/>' % BG,
        '<rect x="0.5" y="0.5" width="%d" height="%d" rx="10" fill="none" stroke="#2a2d33"/>' % (width - 1, height - 1),
        '<circle cx="20" cy="20" r="6" fill="#ff5f57"/><circle cx="40" cy="20" r="6" fill="#febc2e"/><circle cx="60" cy="20" r="6" fill="#28c840"/>',
        '<text x="%d" y="24" fill="#6b717c" text-anchor="middle" style="font-size:12px">%s</text>' % (width // 2, escape(title)),
    ]
    for i, line in enumerate(lines):
        y = pad_top + i * line_h + font_size
        spans = []
        for chunk, st in parse(line):
            attrs = ['fill="%s"' % st.get("fg", FG)]
            if st.get("bold"):
                attrs.append('font-weight="700"')
            if st.get("dim"):
                attrs.append('opacity="0.6"')
            if st.get("italic"):
                attrs.append('font-style="italic"')
            spans.append("<tspan %s>%s</tspan>" % (" ".join(attrs), escape(chunk)))
        out.append('<text x="%d" y="%.1f" xml:space="preserve">%s</text>' % (pad_x, y, "".join(spans)))
    out.append("</svg>")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="terminal")
    ap.add_argument("--cols", type=int)
    args = ap.parse_args()
    sys.stdout.write(render(sys.stdin.read(), args.title, args.cols))


if __name__ == "__main__":
    main()
