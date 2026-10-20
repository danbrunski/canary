# ruff: noqa: E501
"""CANary cap bank rev 1.0 -- parts and nets (design record: docs/CANary-PWR-Spec.md, P4).

Four 25 F cells in series, the NTC, and the lead's header; no active parts. Cell 1 is the stack's
bottom cell (LTC3350 CAPRTN..CAP1), cell 4 its top (CAP3..CAP4). The connector's pin map is the
lead's (hardware/tools/capbank_lead.py), shared with CANary-PWR's J4.

Each part is a dict like CANary-PWR's design.py: ref, lib_id, value, {pin: net}, footprint and BOM fields.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import capbank_lead  # noqa: E402

CELL_FP = "canary-capbank:EDLC_Radial_D16.0mm_P7.50mm_Horizontal"
# A 3.0 V cell run at 2.5 V: the 2.7 V cells are all derated to 2.3 V at 85 C, the TVA to 2.5 V. It is
# also the one candidate that is AEC-Q200 and rated past the 4 A charge current (4.2 A at a 15 C rise).
CELL = dict(
    value="25F 3.0V",
    mpn="TVA1625-3R0256-R",
    mfr="Eaton",
    ds="https://www.eaton.com/content/dam/eaton/products/electronic-components/resources/data-sheet/eaton-tva-automotive-supercapacitor-cylindrical-cell-data-sheet-elx1087-en.pdf",
    desc="EDLC cell, 25 F 3.0 V, 16.5 x 28.9 mm max radial, AEC-Q200; laid flat and tied down",
    note="Run at 2.5 V (CAPFB on CANary-PWR sets 9.98 V), the TVA's limit at 85 C. Alternate: UCAP BCAP0025 P300 X11 (not AEC-Q200). Longer lead is +.",
)


def part(ref, lib_id, value, pins, fp, mpn="", mfr="", ds="", desc="", note="", rot=0):
    return dict(
        ref=ref,
        lib_id=lib_id,
        value=value,
        pins=pins,
        fp=fp,
        mpn=mpn,
        mfr=mfr,
        ds=ds,
        desc=desc,
        note=note,
        rot=rot,
    )


# Series string, bottom to top: (ref, + net, - net). GND is the stack's bottom plate, as on CANary-PWR.
STRING = (
    ("C1", "BANK_TAP1", "GND"),
    ("C2", "BANK_TAP2", "BANK_TAP1"),
    ("C3", "BANK_TAP3", "BANK_TAP2"),
    ("C4", "VCAP", "BANK_TAP3"),
)

PARTS = [
    part(
        "J1",
        capbank_lead.HEADER["lib_id"],
        "Cap bank lead",
        dict(capbank_lead.PINS),
        capbank_lead.HEADER["fp"],
        mpn=capbank_lead.HEADER["mpn"],
        mfr=capbank_lead.HEADER["mfr"],
        desc="Micro-Fit 3.0 2x5 right-angle header; the lead is 43025-1000 at both ends, wired pin n to pin n",
        note="Same header and pin map as CANary-PWR J4 (hardware/tools/capbank_lead.py)",
    ),
    *(
        part(
            ref,
            "Device:C_Polarized",
            CELL["value"],
            {"1": pos, "2": neg},
            CELL_FP,
            CELL["mpn"],
            CELL["mfr"],
            CELL["ds"],
            CELL["desc"],
            CELL["note"],
        )
        for ref, pos, neg in STRING
    ),
    part(
        "TH1",
        "Device:Thermistor_NTC",
        "10k NTC",
        {"1": "LTC_GPI", "2": "GND"},
        "Resistor_SMD:R_0603_1608Metric",
        mpn="NTCS0603E3103FLT",
        mfr="Vishay",
        ds="https://www.vishay.com/docs/29056/ntcs0603e3t.pdf",
        desc="NTC thermistor 10 k 1 % at 25 C, B25/85 3435 K, AEC-Q200; on cell 1's - lead copper",
        note="Read by the LTC3350's GPI against 10 k 0.1 % from INTVCC on CANary-PWR. Same curve within 0.3 % above 0 C: Murata NCU18XH103F6SRB, TDK NTCG163JF103FT1S.",
    ),
    part(
        "NT1",
        "Device:NetTie_2",
        "Kelvin +",
        {"1": "BANK_POS_S", "2": "VCAP"},
        "NetTie:NetTie-2_SMD_Pad0.5mm",
        desc="Stack + sense joins VCAP at cell 4's + lead, not along the power copper",
    ),
    part(
        "NT2",
        "Device:NetTie_2",
        "Kelvin -",
        {"1": "BANK_NEG_S", "2": "GND"},
        "NetTie:NetTie-2_SMD_Pad0.5mm",
        desc="Stack - sense joins GND at cell 1's - lead, not along the power copper",
    ),
]
