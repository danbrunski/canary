# ruff: noqa: E501, N802, N806, E741
"""Write the CANary-PWR project symbol library (KiCad 10 syntax)."""

FONT = "\t" * 0  # placeholder for readability


def _eff(ind, hide=False, justify=None):
    t = "\t" * ind
    j = f"\n{t}\t(justify {justify})" if justify else ""
    return f"{t}(effects\n{t}\t(font\n{t}\t\t(size 1.27 1.27)\n{t}\t){j}\n{t})"


def _prop(name, value, x, y, ang=0, hide=False, ind=2):
    t = "\t" * ind
    h = f"\n{t}\t(hide yes)" if hide else ""
    return (
        f'{t}(property "{name}" "{value}"\n{t}\t(at {x} {y} {ang})\n{t}\t(show_name no)\n'
        f"{t}\t(do_not_autoplace no){h}\n{_eff(ind + 1)}\n{t})"
    )


def _pin(ptype, x, y, ang, name, num, hidden=False, length=2.54):
    t = "\t\t\t"
    h = f"\n{t}\t(hide yes)" if hidden else ""
    return (
        f"{t}(pin {ptype} line\n{t}\t(at {x} {y} {ang})\n{t}\t(length {length}){h}\n"
        f'{t}\t(name "{name}"\n{_eff(5)}\n{t}\t)\n{t}\t(number "{num}"\n{_eff(5)}\n{t}\t)\n{t})'
    )


def ic_symbol(
    name,
    ref,
    value,
    footprint,
    datasheet,
    desc,
    left,
    right,
    top=(),
    bottom=(),
    width=15.24,
    hidden=(),
    keywords="",
):
    """left/right/top/bottom: lists of (num, name, type) or None for a gap. hidden: (num, name, type)."""
    rows = max(len(left), len(right))
    h = rows * 2.54 + 2.54
    top_y = round(h / 2 / 2.54) * 2.54
    bot_y = top_y - h
    xl, xr = -width / 2, width / 2
    pins = []
    for i, p in enumerate(left):
        if p:
            pins.append(
                _pin(p[2], round(xl - 2.54, 2), round(top_y - 2.54 * (i + 1), 2), 0, p[1], p[0])
            )
    for i, p in enumerate(right):
        if p:
            pins.append(
                _pin(p[2], round(xr + 2.54, 2), round(top_y - 2.54 * (i + 1), 2), 180, p[1], p[0])
            )
    n = len(top)
    for i, p in enumerate(top):
        if p:
            x = round((i - (n - 1) / 2) * 2.54, 2)
            pins.append(_pin(p[2], x, round(top_y + 2.54, 2), 270, p[1], p[0]))
    n = len(bottom)
    for i, p in enumerate(bottom):
        if p:
            x = round((i - (n - 1) / 2) * 2.54, 2)
            pins.append(_pin(p[2], x, round(bot_y - 2.54, 2), 90, p[1], p[0]))
    for p in hidden:
        pins.append(_pin(p[2], 0, 0, 0, p[1], p[0], hidden=True, length=0))
    body = (
        f'\t\t(symbol "{name}_0_1"\n\t\t\t(rectangle\n\t\t\t\t(start {xl} {top_y})\n\t\t\t\t(end {xr} {round(bot_y, 2)})\n'
        f"\t\t\t\t(stroke\n\t\t\t\t\t(width 0.254)\n\t\t\t\t\t(type default)\n\t\t\t\t)\n"
        f"\t\t\t\t(fill\n\t\t\t\t\t(type background)\n\t\t\t\t)\n\t\t\t)\n\t\t)"
    )
    props = "\n".join(
        [
            _prop("Reference", ref, xl, round(top_y + 1.27 + (2.54 if top else 0), 2), 0),
            _prop("Value", value, xl, round(bot_y - 1.27 - (2.54 if bottom else 0), 2), 0),
            _prop("Footprint", footprint, 0, 0, 0, hide=True),
            _prop("Datasheet", datasheet, 0, 0, 0, hide=True),
            _prop("Description", desc, 0, 0, 0, hide=True),
            _prop("ki_keywords", keywords or name, 0, 0, 0, hide=True),
        ]
    )
    return (
        f'\t(symbol "{name}"\n\t\t(pin_names\n\t\t\t(offset 1.016)\n\t\t)\n\t\t(exclude_from_sim no)\n'
        f"\t\t(in_bom yes)\n\t\t(on_board yes)\n\t\t(in_pos_files yes)\n"
        f"\t\t(duplicate_pin_numbers_are_jumpers no)\n{props}\n{body}\n"
        f'\t\t(symbol "{name}_1_1"\n' + "\n".join(pins) + "\n\t\t)\n\t\t(embedded_fonts no)\n\t)"
    )


def library():
    syms = []
    syms.append(
        ic_symbol(
            "TCA4307",
            "U",
            "TCA4307DGKR",
            "Package_SO:VSSOP-8_3x3mm_P0.65mm",
            "https://www.ti.com/lit/ds/symlink/tca4307.pdf",
            "Hot-swappable I2C buffer with stuck-bus recovery; powered-off high-impedance I/O; VSSOP-8 (DGK)",
            left=[
                ("6", "SDAIN", "bidirectional"),
                ("3", "SCLIN", "bidirectional"),
                None,
                ("1", "EN", "input"),
            ],
            right=[
                ("7", "SDAOUT", "bidirectional"),
                ("2", "SCLOUT", "bidirectional"),
                None,
                ("5", "READY", "open_collector"),
            ],
            top=[("8", "VCC", "power_in")],
            bottom=[("4", "GND", "power_in")],
            width=12.7,
        )
    )
    syms.append(
        ic_symbol(
            "TPS1HB35-Q1",
            "U",
            "TPS1HB35AQPWPRQ1",
            "Package_SO:HTSSOP-16-1EP_4.4x5mm_P0.65mm_EP3.4x5mm_Mask2.78x3.37mm",
            "https://www.ti.com/lit/ds/symlink/tps1hb35-q1.pdf",
            "40V 35mOhm single-channel smart high-side switch, adjustable current limit, analog current sense; HTSSOP-16 PowerPAD (PWP0016M), EP = VBB",
            left=[
                ("17", "VBB", "power_in"),
                None,
                ("4", "EN", "input"),
                ("16", "DIA_EN", "input"),
                ("3", "LATCH", "input"),
                ("14", "SEL1", "input"),
            ],
            right=[
                ("6", "VOUT", "power_out"),
                ("7", "VOUT", "passive"),
                ("8", "VOUT", "passive"),
                None,
                ("2", "SNS", "output"),
                ("5", "ILIM", "passive"),
            ],
            bottom=[("1", "GND", "power_in")],
            hidden=[
                ("9", "NC", "no_connect"),
                ("10", "NC", "no_connect"),
                ("11", "NC", "no_connect"),
                ("12", "NC", "no_connect"),
                ("13", "NC", "no_connect"),
                ("15", "NC", "no_connect"),
            ],
            width=15.24,
        )
    )
    syms.append(
        ic_symbol(
            "TPS7A1633-Q1",
            "U",
            "TPS7A1633QDGNRQ1",
            "Package_SO:Texas_DGN0008B_VSSOP-8-1EP_3x3mm_P0.65mm_EP2x3mm_Mask1.88x1.98mm",
            "https://www.ti.com/lit/ds/symlink/tps7a16-q1.pdf",
            "60V 100mA 5uA-Iq LDO, fixed 3.3V, power good; HVSSOP-8 (DGN)",
            left=[("8", "IN", "power_in"), ("5", "EN", "input"), ("6", "NC", "no_connect")],
            right=[
                ("1", "OUT", "power_out"),
                ("3", "PG", "open_collector"),
                ("7", "DELAY", "passive"),
                ("2", "FB/NC", "passive"),
            ],
            bottom=[("4", "GND", "power_in"), ("9", "EP", "passive")],
            width=15.24,
        )
    )
    return (
        '(kicad_symbol_lib\n\t(version 20251024)\n\t(generator "kicad_symbol_editor")\n'
        '\t(generator_version "10.0")\n' + "\n".join(syms) + "\n)\n"
    )


if __name__ == "__main__":
    import sys

    open(sys.argv[1], "w").write(library())
    print("wrote", sys.argv[1])
