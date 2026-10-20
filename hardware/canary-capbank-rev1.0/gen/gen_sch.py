# ruff: noqa: E501, N806
"""Write the cap bank's one-sheet schematic, label-connected like CANary-PWR's.

The KiCad 10 s-expression writers are CANary-PWR's (canary-pwr-rev1.0/gen/gen_sch.py); this file only lays
out the bank's seven symbols. Usage: gen_sch.py <project_dir>. Prints the root sheet's UUID.
"""

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bank  # noqa: E402

# Loaded under its own name: this file is gen_sch.py too.
_spec = importlib.util.spec_from_file_location(
    "pwr_gen_sch", os.path.join(HERE, "..", "..", "canary-pwr-rev1.0", "gen", "gen_sch.py")
)
kgen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kgen)

PROJECT = "canary-capbank"
CELLS = {ref for ref, _, _ in bank.STRING}
TAPS = {neg for _, _, neg in bank.STRING if neg != "GND"}
kgen.PROJECT = PROJECT  # symbol_instance writes it into every instance block


class SystemLibs(kgen.Libs):
    """The bank uses only KiCad's own symbol libraries."""

    def __init__(self):
        self.paths = {
            fn[:-10]: os.path.join(kgen.SYSLIB, fn)
            for fn in os.listdir(kgen.SYSLIB)
            if fn.endswith(".kicad_sym")
        }
        self.cache = {}


# Symbol origins on an A4 sheet (mm, y down): the header on the left, the string top to bottom on the
# right with cell 4 at the top, the Kelvin ties beside the string's ends, the NTC between.
AT = {
    "J1": (63.5, 99.06),
    "C4": (165.1, 76.2),
    "C3": (165.1, 91.44),
    "C2": (165.1, 106.68),
    "C1": (165.1, 121.92),
    "NT1": (228.6, 76.2),
    "NT2": (228.6, 121.92),
    "TH1": (114.3, 121.92),
}
FLAG_AT = (114.3, 147.32)
# Where a ground symbol's value text goes, by the direction its pin points: beyond the symbol's tip.
GND_TEXT = {0: (6.35, 0), 90: (0, -3.8), 180: (-6.35, 0), 270: (0, 3.8)}

NOTES = (
    "Four 25 F cells in series, run at 2.5 V each (10 V stack), charged and balanced by CANary-PWR's LTC3350.\n"
    "Cell 1 is the bottom cell (LTC3350 CAPRTN..CAP1), cell 4 the top (CAP3..CAP4). GND is the stack's bottom plate.\n"
    "Inter-cell joins carry the full 4 A (7.25 A peak): pours on both layers. The taps to J1 carry only the shunt current (<= 0.5 A).\n"
    "Kelvin sense: NT1 and NT2 join BANK_POS_S / BANK_NEG_S to the stack at the cell leads themselves.\n"
    "The lead: 43025-1000 at both ends, wired pin n to pin n; pin map in hardware/tools/capbank_lead.py, shared with CANary-PWR J4.\n"
    "DISCHARGE BEFORE UNPLUGGING: lower vshunt on CANary-PWR (bench button held, or I2C) until the red LED goes out."
)


def main(projdir):
    libs = SystemLibs()
    root_uuid = kgen.U()
    path = f"/{root_uuid}"
    body, used = [], {}
    body.append(
        kgen.text_note(
            "CANary cap bank rev 1.0 -- four 25 F cells for CANary-PWR's LTC3350",
            20.32,
            22.86,
            2.54,
        )
    )
    body.append(kgen.text_note(NOTES, 20.32, 55.88, 1.524))  # anchored at its last line
    pwr_n = 0
    for p in bank.PARTS:
        X, Y = AT[p["ref"]]
        geo = kgen.part_geometry(libs, p)
        used[p["lib_id"]] = True
        fields = [("Footprint", p["fp"]), ("Datasheet", p["ds"]), ("Description", p["desc"])]
        fields += [
            (k, p[f])
            for k, f in (("MPN", "mpn"), ("Manufacturer", "mfr"), ("Note", "note"))
            if p[f]
        ]
        bx0, by0, bx1, by1 = libs.bbox(p["lib_id"])
        side = X + max(abs(bx0), abs(bx1)) + 7.62  # centred text, clear of the symbol
        body.append(
            kgen.symbol_instance(
                p["lib_id"],
                X,
                Y,
                p["rot"],
                p["ref"],
                p["value"],
                fields,
                sorted({g["num"] for g in geo}, key=lambda n: (len(n), n)),
                path,
                ref_pos=(X, Y - max(abs(by0), abs(by1)) - 4.0)
                if p["ref"].startswith(("J", "NT"))
                else (side, Y - 1.27),
                val_pos=(X, Y + max(abs(by0), abs(by1)) + 4.0)
                if p["ref"].startswith(("J", "NT"))
                else (side, Y + 1.27),
                in_bom=p["lib_id"] != "Device:NetTie_2",  # copper only, as the library symbol says
            )
        )
        for g in geo:
            net = p["pins"][g["num"]]
            ax, ay = round(X + g["sx"], 4), round(Y + g["sy"], 4)
            if net in TAPS and p["ref"] in CELLS:
                continue  # drawn as the wire between two cells, below
            if net == "GND":
                tx, ty = GND_TEXT[int(round(g["out"])) % 360]
                pwr_n += 1
                used["power:GND"] = True
                body.append(
                    kgen.symbol_instance(
                        "power:GND",
                        ax,
                        ay,
                        (g["out"] - 270) % 360,
                        f"#PWR{pwr_n:03d}",
                        "GND",
                        [("Footprint", ""), ("Datasheet", ""), ("Description", "")],
                        ["1"],
                        path,
                        ref_pos=(ax, ay + 5),
                        val_pos=(ax + tx, ay + ty),
                    )
                )
                continue
            body.append(kgen.label_text("global", net, ax, ay, g["out"]))
    # The string: a wire from each cell's - pin to the next cell down's + pin, labelled with its tap.
    for (upper, _, tap), (lower, _, _) in zip(bank.STRING[::-1], bank.STRING[-2::-1], strict=False):
        x, y0 = AT[upper][0], AT[upper][1] + 3.81
        y1 = AT[lower][1] - 3.81
        body.append(
            f"\t(wire\n\t\t(pts\n\t\t\t(xy {kgen.fmt(x)} {kgen.fmt(y0)}) (xy {kgen.fmt(x)} {kgen.fmt(y1)})\n\t\t)\n"
            f'\t\t(stroke\n\t\t\t(width 0)\n\t\t\t(type default)\n\t\t)\n\t\t(uuid "{kgen.U()}")\n\t)'
        )
        body.append(kgen.label_text("global", tap, x, (y0 + y1) / 2, 0))
    # GND has power inputs (the GND symbols) and only passive pins to drive it.
    fx, fy = FLAG_AT
    used["power:PWR_FLAG"] = True
    body.append(
        kgen.symbol_instance(
            "power:PWR_FLAG",
            fx,
            fy,
            0,
            "#FLG001",
            "PWR_FLAG",
            [("Footprint", ""), ("Datasheet", "~"), ("Description", "")],
            ["1"],
            path,
            ref_pos=(fx, fy - 5),
            val_pos=(fx, fy - 4),
        )
    )
    pwr_n += 1
    body.append(
        kgen.symbol_instance(
            "power:GND",
            fx,
            fy,
            0,
            f"#PWR{pwr_n:03d}",
            "GND",
            [("Footprint", ""), ("Datasheet", ""), ("Description", "")],
            ["1"],
            path,
            ref_pos=(fx, fy + 5),
            val_pos=(fx, fy + 3.8),
        )
    )
    body.append('\t(sheet_instances\n\t\t(path "/"\n\t\t\t(page "1")\n\t\t)\n\t)')
    txt = kgen.sheet_file(
        root_uuid,
        "A4",
        "CANary cap bank",
        [
            "Four 25 F / 2.7 V cells, NTC, Kelvin-sensed stack, Micro-Fit 2x5 lead to CANary-PWR J4",
            "Design record: docs/CANary-PWR-Spec.md P4",
            "Generated by gen/gen_sch.py; label-connected",
        ],
        [kgen.lib_symbol_text(libs.flat(lid)) for lid in sorted(used)],
        body,
    )
    open(os.path.join(projdir, f"{PROJECT}.kicad_sch"), "w").write(txt)
    return root_uuid


if __name__ == "__main__":
    print(main(sys.argv[1]))
