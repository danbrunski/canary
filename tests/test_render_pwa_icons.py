"""scripts/render_pwa_icons.py: the PNG app icons drawn from canary.svg (P2.1)."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from scripts.render_pwa_icons import PUBLIC, render

AMBER = (255, 184, 74, 255)


def test_plain_icon_keeps_the_rounded_corners() -> None:
    image = render(PUBLIC / "canary.svg", 64, full_bleed=False, art_scale=1.0)
    assert image.size == (64, 64)
    assert cast(tuple[int, ...], image.getpixel((0, 0)))[3] == 0  # transparent corner
    assert image.getpixel((32, 8)) == AMBER


def test_maskable_icon_is_full_bleed() -> None:
    image = render(PUBLIC / "canary.svg", 64, full_bleed=True, art_scale=0.8)
    assert image.getpixel((0, 0)) == AMBER
    assert image.getpixel((63, 63)) == AMBER


def test_unsupported_svg_is_refused(tmp_path: Path) -> None:
    svg = tmp_path / "icon.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<ellipse cx="32" cy="32" rx="10" ry="5"/></svg>'
    )
    with pytest.raises(ValueError, match="ellipse"):
        render(svg, 32, full_bleed=False, art_scale=1.0)
