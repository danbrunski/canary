"""Write the BOM and position file PCBWay's KiCad plugin would upload for a board.

Run with KiCad's own python (it needs `pcbnew`, see README.md):

    pcbway_bom.py <board.kicad_pcb> <out_dir>

It writes `PCBWay_bom.csv` and `PCBWay_positions.csv` into out_dir, byte for byte what the plugin
(com_github_pcbway_PCBWay-Plug-in-for-Kicad, `process.py` `get_components_file`) puts in the zip it
uploads. Checked against the plugin's own output for the cap bank (2026-10-20). Use it to read the
BOM before an order, or to upload the files by hand.

The plugin builds its BOM from the footprints' fields, not the schematic's. One line per value +
footprint + package + MPN, and one line per do-not-populate part. MPN comes from a field named
MPN (or mpn, Part Number ...), Package from Package (or pack, case ...). Every other non-empty field
is a column, Reference, Value, Datasheet, Footprint and ki_fp_filters excepted. A field that differs
inside a line reads "[C1]text; [C2,C3]other". The plugin groups those values with itertools.groupby,
which joins only neighbouring refs, so refs with a repeated value can drop out of that text. A field
set on one part of a line reads as if it applied to the whole line. This copy keeps both quirks.
"""

import csv
import os
import sys
from itertools import groupby
from typing import Any

import pcbnew

MPN_KEYS = [
    "mpn",
    "MPN",
    "Mpn",
    "PCBWay_MPN",
    "part number",
    "Part Number",
    "Part No.",
    "Mfr. Part No.",
    "Mfg Part",
    "Manufacturer_Part_Number",
]
PACK_KEYS = ["pack", "PACK", "Pack", "package", "PACKAGE", "Package", "case", "CASE", "Case"]
DNP_KEYS = ["dnp", "Dnp", "DNp", "DNP", "dNp", "dNP", "dnP", "DnP"]
NO_SHOW = ["ki_fp_filters", "DNP", "Reference", "Value", "Datasheet", "Footprint"]
IGNORED = MPN_KEYS + PACK_KEYS + NO_SHOW


def _first_field(fp: "pcbnew.FOOTPRINT", keys: list[str]) -> str:
    for k in keys:
        if fp.HasField(k):
            return str(fp.GetFieldText(k) or "")
    return ""


def _has(attrs: int, flag: int) -> bool:
    return attrs & flag == flag


def read_board(board: "pcbnew.BOARD") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The plugin's per-footprint records: (position rows, BOM items)."""
    aux = board.GetDesignSettings().GetAuxOrigin()
    positions, items = [], []
    for fp in sorted(board.GetFootprints(), key=lambda f: f.GetReference()):
        attrs = fp.GetAttributes()
        not_in_bom = _has(attrs, pcbnew.FP_EXCLUDE_FROM_BOM)
        not_in_pos = _has(attrs, pcbnew.FP_EXCLUDE_FROM_POS_FILES)
        if not_in_bom and not_in_pos:
            continue
        name = str(fp.GetFPID().GetLibItemName())
        mount = "smt" if _has(attrs, pcbnew.FP_SMD) else "tht"
        mpn, pack = _first_field(fp, MPN_KEYS), _first_field(fp, PACK_KEYS)
        dnp = any(fp.HasField(k) for k in DNP_KEYS) or fp.GetValue().upper() == "DNP" or fp.IsDNP()
        if not not_in_pos:
            positions.append(
                {
                    "pos_x": (fp.GetPosition()[0] - aux[0]) / 1000000.0,
                    "pos_y": (fp.GetPosition()[1] - aux[1]) * -1.0 / 1000000.0,
                    "rotation": fp.GetOrientation().AsDegrees(),
                    "side": {pcbnew.F_Cu: "top", pcbnew.B_Cu: "bottom"}.get(fp.GetLayer()),
                    "designator": fp.GetReference(),
                    "mpn": mpn,
                    "pack": pack,
                    "footprint": name,
                    "value": fp.GetValue(),
                    "mount_type": mount,
                }
            )
        if not_in_bom:
            continue
        item = {
            "designator": fp.GetReference(),
            "value": fp.GetValue(),
            "footprint": name,
            "pack": pack,
            "mpn": mpn,
            "DNP": "Yes" if dnp else "",
            "Mount_Type": mount,
        }
        for k, v in fp.GetFieldsText().items():
            if k.upper() in ("DNP", "MOUNT_TYPE"):
                k = "Custom_" + k
            if v and k not in IGNORED:
                item[k] = v
        items.append(item)
    return positions, items


def bom_rows(items: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    """Group the items into BOM lines as the plugin does: (columns, rows)."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        key = "_".join((it["value"], it["footprint"], it["pack"], it["mpn"]))
        if it["DNP"]:
            key = it["designator"] + "_" + key
        groups.setdefault(key, []).append(it)
    fixed = ["designator", "quantity", "value", "footprint", "pack", "mpn"]
    columns = ["Designator", "Quantity", "Value", "Footprint", "Package", "MPN"]
    rows = []
    for members in groups.values():
        values: dict[str, list[tuple[str, str]]] = {}
        for it in members:
            for k, v in it.items():
                if k in fixed:
                    continue
                if k not in columns:
                    columns.append(k)
                values.setdefault(k, []).append((it["designator"], v))
        first = members[0]
        row = {
            "Designator": ", ".join(it["designator"] for it in members),
            "Quantity": len(members),
            "Value": first["value"],
            "Footprint": first["footprint"],
            "Package": first["pack"],
            "MPN": first["mpn"],
        }
        for k, pairs in values.items():
            by_value = {v: [ref for ref, _ in g] for v, g in groupby(pairs, key=lambda p: p[1])}
            if len(by_value) > 1:
                row[k] = "; ".join(f"[{','.join(refs)}]{v}" for v, refs in by_value.items())
            else:
                row[k] = pairs[0][1]
        rows.append(row)
    return columns, rows


def main() -> None:
    board_path, out_dir = sys.argv[1:3]
    positions, items = read_board(pcbnew.LoadBoard(board_path))
    columns, rows = bom_rows(items)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "PCBWay_bom.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(columns)
        # the plugin leaves out unannotated parts ("R**")
        w.writerows(
            [row.get(c, "") for c in columns] for row in rows if "**" not in row["Designator"]
        )
    with open(
        os.path.join(out_dir, "PCBWay_positions.csv"), "w", newline="", encoding="utf-8-sig"
    ) as f:
        w = csv.writer(f)
        w.writerow(positions[0].keys())
        w.writerows(p.values() for p in positions if "**" not in p["designator"])
    parts = sum(r["Quantity"] for r in rows)
    print(f"{len(rows)} BOM lines, {parts} parts, {len(positions)} placements -> {out_dir}")


if __name__ == "__main__":
    main()
