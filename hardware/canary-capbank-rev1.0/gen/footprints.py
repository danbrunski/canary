# ruff: noqa: E501
"""Write the cap bank's footprint library: a 16 mm radial EDLC cell laid flat.

The cell lies on the board with its leads bent down 90 degrees into two holes, 7.5 mm apart across the
cell's axis. Pad 1 (+) is the origin, pad 2 (-) is 7.5 mm along +y, and the body runs along -x from an end
face HOLE_TO_BODY from the holes. The outline is the largest of the candidates (Eaton TVA1625: 16.5 x 28.9
mm max; Kyocera AVX: 17.0 mm max diameter), so any of them fits the same board.

Usage: footprints.py <lib/footprints.pretty>
"""

import os
import sys

PITCH = 7.5  # lead spacing; every 16 mm 25 F cell (AVX's 7.3 +/-0.5 included)
DRILL = 1.2  # 0.8 mm leads, bent by hand: room for the bend's position
PAD = 3.0  # wider than KiCad's 2.4: the string's full current enters the pours here
HOLE_TO_BODY = 4.5  # 2 mm of straight lead out of the seal, then a 2 mm bend radius
DIA = 17.0  # body envelope
LEN = 29.0
CRT = 0.25  # courtyard margin
NAME = "EDLC_Radial_D16.0mm_P7.50mm_Horizontal"


def f(v):
    return f"{v:.4f}".rstrip("0").rstrip(".")


def line(layer, a, b, width):
    return (
        f"\t(fp_line\n\t\t(start {f(a[0])} {f(a[1])})\n\t\t(end {f(b[0])} {f(b[1])})\n"
        f'\t\t(stroke\n\t\t\t(width {f(width)})\n\t\t\t(type solid)\n\t\t)\n\t\t(layer "{layer}")\n\t)'
    )


def rect(layer, x0, y0, x1, y1, width):
    return "\n".join(
        line(layer, a, b, width)
        for a, b in (
            ((x0, y0), (x1, y0)),
            ((x1, y0), (x1, y1)),
            ((x1, y1), (x0, y1)),
            ((x0, y1), (x0, y0)),
        )
    )


def poly(layer, pts, width):
    xy = " ".join(f"(xy {f(x)} {f(y)})" for x, y in pts)
    return (
        f"\t(fp_poly\n\t\t(pts {xy})\n\t\t(stroke\n\t\t\t(width {f(width)})\n\t\t\t(type solid)\n\t\t)\n"
        f'\t\t(fill no)\n\t\t(layer "{layer}")\n\t)'
    )


def text(kind, txt, x, y, layer, size=1.0, hide=False):
    head = (
        f'\t(property "{kind}" "{txt}"'
        if kind in ("Reference", "Value")
        else f'\t(fp_text user "{txt}"'
    )
    return (
        f'{head}\n\t\t(at {f(x)} {f(y)} 0)\n\t\t(layer "{layer}")\n'
        + ("\t\t(hide yes)\n" if hide else "")
        + f"\t\t(effects\n\t\t\t(font\n\t\t\t\t(size {f(size)} {f(size)})\n\t\t\t\t(thickness {f(size * 0.15)})\n\t\t\t)\n\t\t)\n\t)"
    )


def pad(num, x, y, shape):
    extra = "\t\t(roundrect_rratio 0.1)\n" if shape == "roundrect" else ""
    return (
        f'\t(pad "{num}" thru_hole {shape}\n\t\t(at {f(x)} {f(y)})\n\t\t(size {f(PAD)} {f(PAD)})\n'
        f'\t\t(drill {f(DRILL)})\n\t\t(layers "*.Cu" "*.Mask")\n\t\t(remove_unused_layers no)\n{extra}\t)'
    )


def cell():
    cy = PITCH / 2  # the cell's axis, between the two leads
    end = -HOLE_TO_BODY  # end face, where the leads leave the seal
    far = end - LEN  # the vent end
    r = DIA / 2
    tab = PAD / 2 + CRT
    parts = [
        f'(footprint "{NAME}"\n\t(version 20260206)\n\t(generator "canary-capbank-gen")\n\t(layer "F.Cu")',
        f'\t(descr "Radial EDLC lying flat, leads bent 90 deg into the board; body up to D{f(DIA)} x L{f(LEN)} mm, '
        f'end face {f(HOLE_TO_BODY)} mm from the holes, lead pitch {f(PITCH)} mm, 0.8 mm leads. Vent at the far end: keep it clear")',
        '\t(tags "supercapacitor EDLC radial horizontal flat")',
        text("Reference", "REF**", (end + far) / 2, cy, "F.SilkS"),
        text("Value", NAME, (end + far) / 2, cy + 2, "F.Fab"),
        "\t(attr through_hole)\n\t(duplicate_pad_numbers_are_jumpers no)",
        # Fab: the body envelope, the leads from the end face to the holes, polarity and the vent.
        rect("F.Fab", far, cy - r, end, cy + r, 0.1),
        line("F.Fab", (end, 0), (0, 0), 0.1),
        line("F.Fab", (end, PITCH), (0, PITCH), 0.1),
        text("", "+", end + 1.2, -1.2, "F.Fab"),
        text("", "VENT", far + 3, cy, "F.Fab", 0.8),
        text("", "${REFERENCE}", (end + far) / 2, cy - 2, "F.Fab"),
        # Silk: the body envelope (far from the pads), a + beside pad 1 and a bar on the - side of the body.
        rect("F.SilkS", far - 0.12, cy - r - 0.12, end + 0.12, cy + r + 0.12, 0.12),
        line("F.SilkS", (far + 1, cy + r - 1.2), (end - 1, cy + r - 1.2), 0.3),
        text("", "+", -2.6, -1.6, "F.SilkS"),
        # Courtyard: the body, and the leads' tab out to the pads.
        poly(
            "F.CrtYd",
            [
                (far - CRT, cy - r - CRT),
                (end + CRT, cy - r - CRT),
                (end + CRT, -tab),
                (tab, -tab),
                (tab, PITCH + tab),
                (end + CRT, PITCH + tab),
                (end + CRT, cy + r + CRT),
                (far - CRT, cy + r + CRT),
            ],
            0.05,
        ),
        pad("1", 0, 0, "roundrect"),
        pad("2", 0, PITCH, "circle"),
        "\t(embedded_fonts no)\n)\n",
    ]
    return "\n".join(parts)


def main(libdir):
    os.makedirs(libdir, exist_ok=True)
    with open(os.path.join(libdir, NAME + ".kicad_mod"), "w") as fh:
        fh.write(cell())
    print(f"footprint {NAME} written")


if __name__ == "__main__":
    main(sys.argv[1])
