"""Generate the app's icons (favicon, home-screen icons) from one drawing.

    uv run python scripts/make_icons.py

The icon is a white receipt with a zigzag tear-off edge on the app's green. Shapes are defined once on a
100x100 grid and written both as SVG and, via Pillow, as PNG/ICO, so every size matches.
"""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "src" / "receipt_scanner" / "static" / "icons"

GREEN = "#1f6f5c"   # --accent in index.html
PAPER = "#ffffff"
INK = "#b7c9c2"     # printed lines
TOTAL = "#1f6f5c"   # the bold "total" line


def receipt(scale: float = 1.0):
    """Receipt outline and its printed lines, scaled about the centre (maskable icons need a margin)."""
    s = lambda x, y: (50 + (x - 50) * scale, 50 + (y - 50) * scale)
    teeth = 5
    left, right, top, bottom, depth = 27, 73, 14, 86, 6
    outline = [s(left, top), s(right, top)]
    step = (right - left) / teeth
    for i in range(teeth * 2 + 1):  # zigzag along the bottom, right to left
        outline.append(s(right - i * step / 2, bottom - (depth if i % 2 else 0)))
    outline.append(s(left, top))
    # (x1, x2, y, thickness, colour); kept bold so they still read at 16px
    lines = [(36, 64, 30, 7, INK), (36, 56, 44, 7, INK), (46, 64, 62, 9, TOTAL)]
    lines = [(*s(x1, y), s(x2, y)[0], t * scale, c) for x1, x2, y, t, c in lines]
    return outline, lines


def svg(rounded: bool) -> str:
    outline, lines = receipt()
    bg = f'<rect width="100" height="100" rx="{22 if rounded else 0}" fill="{GREEN}"/>'
    pts = " ".join(f"{x:g},{y:g}" for x, y in outline)
    body = [bg, f'<polygon points="{pts}" fill="{PAPER}" stroke="{PAPER}" stroke-width="2" stroke-linejoin="round"/>']
    for x1, y, x2, t, c in lines:
        body.append(f'<line x1="{x1:g}" y1="{y:g}" x2="{x2:g}" y2="{y:g}" stroke="{c}" stroke-width="{t:g}" stroke-linecap="round"/>')
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">' + "".join(body) + "</svg>\n"


def png(size: int, rounded: bool, scale: float = 1.0) -> Image.Image:
    ss = 8  # draw large, then shrink, for smooth edges
    px = size * ss / 100
    img = Image.new("RGBA", (size * ss,) * 2, (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, size * ss - 1, size * ss - 1), radius=22 * px if rounded else 0, fill=GREEN)
    outline, lines = receipt(scale)
    d.polygon([(x * px, y * px) for x, y in outline], fill=PAPER)
    for x1, y, x2, t, c in lines:
        r = t * px / 2
        d.rounded_rectangle((x1 * px - r, y * px - r, x2 * px + r, y * px + r), radius=r, fill=c)
    return img.resize((size, size), Image.LANCZOS)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "icon.svg").write_text(svg(rounded=True))            # browser tab icon
    favicon = png(256, rounded=True)
    favicon.save(OUT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])  # for browsers without SVG favicons
    # Home-screen icons are full squares: iOS and Android cut their own rounded shape.
    png(180, rounded=False).convert("RGB").save(OUT / "apple-touch-icon.png", optimize=True)
    for size in (192, 512):
        png(size, rounded=False).convert("RGB").save(OUT / f"icon-{size}.png", optimize=True)
        # Android "maskable": may be cropped to a circle, so keep the receipt inside the middle 80%.
        png(size, rounded=False, scale=0.78).convert("RGB").save(OUT / f"icon-maskable-{size}.png", optimize=True)
    for p in sorted(OUT.iterdir()):
        print(f"{p.name:28} {p.stat().st_size:>6} bytes")


if __name__ == "__main__":
    main()
