# ruff: noqa: E501
"""Check both ends of the cap-bank lead against capbank_lead.py, from the KiCad schematics.

    check_capbank_lead.py [<canary-pwr.kicad_sch> [<canary-capbank.kicad_sch>]]

Defaults to the two projects in this repository. Each schematic is exported to a netlist with kicad-cli
(KICAD_CLI, else the Windows install seen from WSL, else kicad-cli on PATH), and every pin of CANary-PWR's
J4 and the bank's J1 must carry the lead's net, with the lead's header footprint, part number and maker.
The part number matters as much as the pins: a tin header at one end and a gold one at the other would
leave the lead mating gold with tin. The KiCad files are the
source of truth once a board is edited by hand, so this reads them rather than the generators. Exits 1 on
any mismatch.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import capbank_lead  # noqa: E402

HW = os.path.dirname(HERE)
ENDS = (
    ("CANary-PWR", os.path.join(HW, "canary-pwr-rev1.0", "canary-pwr.kicad_sch"), "J4"),
    ("cap bank", os.path.join(HW, "canary-capbank-rev1.0", "canary-capbank.kicad_sch"), "J1"),
)
WIN_CLI = "/mnt/c/Program Files/KiCad/10.0/bin/kicad-cli.exe"


def kicad_cli():
    if os.environ.get("KICAD_CLI"):
        return os.environ["KICAD_CLI"]
    if os.path.exists(WIN_CLI):
        return WIN_CLI
    found = shutil.which("kicad-cli")
    if not found:
        raise SystemExit("kicad-cli not found: set KICAD_CLI")
    return found


def host_path(path):
    """kicad-cli.exe needs a Windows path when run from WSL."""
    if kicad_cli().endswith(".exe"):
        return subprocess.run(
            ["wslpath", "-w", path], capture_output=True, text=True, check=True
        ).stdout.strip()
    return path


def netlist(sch, tmp):
    out = os.path.join(tmp, os.path.basename(sch) + ".xml")
    subprocess.run(
        [
            kicad_cli(),
            "sch",
            "export",
            "netlist",
            "--format",
            "kicadxml",
            "-o",
            host_path(out),
            host_path(sch),
        ],
        capture_output=True,
        check=True,
    )
    return ET.parse(out).getroot()


def check_end(name, root, ref):
    bad = []
    comp = next((c for c in root.iter("comp") if c.get("ref") == ref), None)
    if comp is None:
        return [f"{name}: no {ref}"]
    fp = comp.find("footprint").text if comp.find("footprint") is not None else ""
    if fp != capbank_lead.HEADER["fp"]:
        bad.append(
            f"{name} {ref}: footprint {fp}, the lead's header is {capbank_lead.HEADER['fp']}"
        )
    fields = {f.get("name"): f.text or "" for f in comp.iter("field")}
    for field, key in (("MPN", "mpn"), ("Manufacturer", "mfr")):
        if fields.get(field) != capbank_lead.HEADER[key]:
            bad.append(
                f"{name} {ref}: {field} {fields.get(field)!r}, the lead's header is {capbank_lead.HEADER[key]!r}"
            )
    pins = {}
    for net in root.iter("net"):
        for node in net.iter("node"):
            if node.get("ref") == ref:
                pins[node.get("pin")] = net.get("name").split("/")[-1]  # /Holdup/VCAP -> VCAP
    for pin, want in capbank_lead.PINS.items():
        have = pins.get(pin)
        if have != want:
            bad.append(f"{name} {ref} pin {pin}: {have}, the lead carries {want}")
    extra = sorted(set(pins) - set(capbank_lead.PINS), key=int)
    if extra:
        bad.append(f"{name} {ref}: pins {extra} are not in the lead's map")
    return bad


def main(argv):
    ends = [(n, argv[i] if len(argv) > i else s, r) for i, (n, s, r) in enumerate(ENDS)]
    bad = []
    with tempfile.TemporaryDirectory() as tmp:
        for name, sch, ref in ends:
            bad += check_end(name, netlist(os.path.abspath(sch), tmp), ref)
    for b in bad:
        print(b)
    print(
        f"cap-bank lead: {'MISMATCH' if bad else 'both ends match'} ({len(capbank_lead.PINS)} pins, wired pin n to pin n)"
    )
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
