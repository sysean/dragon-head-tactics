"""Render SVG assets to PNG, and convert text to <path> outlines (no font dependency).

Usage:
  python3 tools/render.py path/to/a.svg [out.png] [width_px]
  python3 tools/render.py --check            # render every svg under assets/
"""
import os
import subprocess
import sys

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.ttLib import TTFont

FONT_CANDIDATES = {
    "bold": "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "black": "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "cjk": "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
}


def render(svg_path, png_path=None, width=1024):
    png_path = png_path or os.path.splitext(svg_path)[0] + ".png"
    subprocess.run(["rsvg-convert", "-w", str(width), svg_path, "-o", png_path], check=True)
    return png_path


def text_to_path(text, size=64, font="bold", letter_spacing=0.0, x=0.0, y=0.0):
    """Return (svg_path_data, total_width) for `text` drawn on a baseline at (x, y)."""
    tt = TTFont(FONT_CANDIDATES[font], fontNumber=0)
    upem = tt["head"].unitsPerEm
    scale = size / upem
    glyphs = tt.getGlyphSet()
    cmap = tt.getBestCmap()
    hmtx = tt["hmtx"]

    pen = SVGPathPen(glyphs)
    # SVGPathPen emits absolute coordinates in font units; accumulate x offsets ourselves.
    from fontTools.pens.transformPen import TransformPen

    cursor = 0.0
    parts = []
    for ch in text:
        name = cmap.get(ord(ch))
        if name is None:
            cursor += size * 0.3
            continue
        tp = TransformPen(pen, (scale, 0, 0, -scale, x + cursor, y))
        glyphs[name].draw(tp)
        cursor += hmtx[name][0] * scale + letter_spacing
    parts.append(pen.getCommands())
    data = " ".join(p for p in parts if p)
    return data, cursor


def measure(text, size=64, font="bold", letter_spacing=0.0):
    tt = TTFont(FONT_CANDIDATES[font], fontNumber=0)
    upem = tt["head"].unitsPerEm
    scale = size / upem
    hmtx = tt["hmtx"]
    cmap = tt.getBestCmap()
    w = 0.0
    for ch in text:
        name = cmap.get(ord(ch))
        if name is None:
            w += size * 0.3
            continue
        w += hmtx[name][0] * scale + letter_spacing
    return w


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "--check":
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
        for name in sorted(os.listdir(root)):
            if name.endswith(".svg"):
                p = os.path.join(root, name)
                print(render(p, os.path.splitext(p)[0] + ".png", 1024))
    else:
        src = args[0]
        out = args[1] if len(args) > 1 else None
        width = int(args[2]) if len(args) > 2 else 1024
        print(render(src, out, width))
