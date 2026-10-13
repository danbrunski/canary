"""Render the dashboard's PNG app icons from ``public/canary.svg`` (phone-ui P2.1).

Android's install prompt wants 192 and 512 px PNGs and a maskable icon; iOS
wants an ``apple-touch-icon``. The SVG stays the source of truth and this
regenerates the PNGs after it changes::

    .venv/bin/python scripts/render_pwa_icons.py

Pillow (already in the dev toolchain) does the drawing, so no SVG renderer is
needed. It understands only what ``canary.svg`` uses -- ``rect``, ``circle``
and ``path`` with absolute ``M``/``L``/``Z`` and flat fills -- and refuses
anything else rather than drawing an icon that silently drops it.

- ``icon-192.png``, ``icon-512.png``: the SVG as drawn (rounded corners).
- ``icon-maskable-512.png``: full-bleed background with the artwork shrunk
  into the central safe zone, since the launcher crops to its own shape.
- ``apple-touch-icon.png`` (180 px): full-bleed, because iOS rounds the
  corners itself and paints transparent ones black.
"""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

from PIL import Image, ImageDraw

PUBLIC = Path(__file__).resolve().parents[1] / "src" / "dashboard" / "static" / "public"
SVG_NS = "{http://www.w3.org/2000/svg}"
#: Drawn this many times larger, then downsampled: Pillow's shapes are aliased.
SUPERSAMPLE = 4
#: The maskable safe zone is a circle of radius 40 % of the icon; at 0.8 the
#: artwork's farthest point sits inside it with room to spare.
MASKABLE_SCALE = 0.8


def _floats(text: str) -> list[float]:
    return [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", text)]


def _path_points(d: str) -> list[tuple[float, float]]:
    tokens = re.findall(r"[A-Za-z]|-?\d+(?:\.\d+)?", d)
    points: list[tuple[float, float]] = []
    numbers: list[float] = []
    for token in tokens:
        if token in ("M", "L"):
            continue
        if token == "Z":
            break
        if token.isalpha():
            raise ValueError(f"path command {token!r} is not supported (absolute M/L/Z only)")
        numbers.append(float(token))
        if len(numbers) == 2:
            points.append((numbers[0], numbers[1]))
            numbers = []
    return points


def parse_svg(path: Path) -> tuple[float, list[tuple[str, dict[str, str]]]]:
    """The viewBox size and the drawable elements, in document order."""
    root = ET.parse(path).getroot()
    view = _floats(root.get("viewBox", ""))
    if len(view) != 4 or view[0] or view[1] or view[2] != view[3]:
        raise ValueError(f"{path.name}: expected a square viewBox at 0 0, got {view}")
    shapes = []
    for element in root:
        tag = element.tag.removeprefix(SVG_NS)
        if tag not in ("rect", "circle", "path"):
            raise ValueError(f"{path.name}: <{tag}> is not supported by this renderer")
        shapes.append((tag, dict(element.attrib)))
    return view[2], shapes


def _draw_shape(
    draw: ImageDraw.ImageDraw,
    tag: str,
    attrs: dict[str, str],
    to_px: Callable[[float, float], tuple[float, float]],
    scale: float,
) -> None:
    fill = attrs.get("fill", "#000000")
    if tag == "rect":
        x, y = float(attrs.get("x", 0)), float(attrs.get("y", 0))
        w, h = float(attrs["width"]), float(attrs["height"])
        radius = float(attrs.get("rx", 0)) * scale
        draw.rounded_rectangle([to_px(x, y), to_px(x + w, y + h)], radius=radius, fill=fill)
    elif tag == "circle":
        cx, cy, r = float(attrs["cx"]), float(attrs["cy"]), float(attrs["r"])
        draw.ellipse([to_px(cx - r, cy - r), to_px(cx + r, cy + r)], fill=fill)
    else:
        draw.polygon([to_px(x, y) for x, y in _path_points(attrs["d"])], fill=fill)


def render(svg: Path, size: int, *, full_bleed: bool, art_scale: float) -> Image.Image:
    """Rasterize ``svg`` at ``size`` px.

    ``full_bleed`` fills the canvas with the first shape's colour (the
    background rect) and draws the rest scaled by ``art_scale`` about the
    centre; otherwise every shape is drawn as the SVG says.
    """
    view, shapes = parse_svg(svg)
    big = size * SUPERSAMPLE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    unit = big / view
    if full_bleed:
        tag, attrs = shapes[0]
        if tag != "rect":
            raise ValueError("full-bleed rendering needs the first shape to be the background rect")
        draw.rectangle([0, 0, big, big], fill=attrs.get("fill", "#000000"))
        shapes = shapes[1:]
    centre = view / 2
    scale = art_scale if full_bleed else 1.0

    def to_px(x: float, y: float) -> tuple[float, float]:
        return (
            (centre + (x - centre) * scale) * unit,
            (centre + (y - centre) * scale) * unit,
        )

    for tag, attrs in shapes:
        _draw_shape(draw, tag, attrs, to_px, unit * scale)
    return image.resize((size, size), Image.Resampling.LANCZOS)


#: (file name, size, full bleed, artwork scale)
ICONS = (
    ("icon-192.png", 192, False, 1.0),
    ("icon-512.png", 512, False, 1.0),
    ("icon-maskable-512.png", 512, True, MASKABLE_SCALE),
    ("apple-touch-icon.png", 180, True, 1.0),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # Resolved here, not as the default: an absolute default would pin this
    # checkout's path into the CLI snapshot.
    parser.add_argument("--public", type=Path, default=None, help="dashboard public/ dir")
    args = parser.parse_args(argv)
    public: Path = args.public or PUBLIC
    svg = public / "canary.svg"
    for name, size, full_bleed, art_scale in ICONS:
        out = public / name
        render(svg, size, full_bleed=full_bleed, art_scale=art_scale).save(out, optimize=True)
        print(f"wrote {out} ({size}x{size})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
