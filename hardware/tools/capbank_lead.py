# ruff: noqa: E501
"""The cap-bank lead: CANary-PWR's J4 to the cap bank's J1 (docs/CANary-PWR-Spec.md, P4).

Both ends are the same Molex Micro-Fit 3.0 2 x 5 right-angle header, and the lead is two 43025-1000
receptacles wired pin n to pin n. Both boards' generators take the pin map from here, so the two ends
cannot be captured differently; check_capbank_lead.py checks the KiCad netlists against it, since the
KiCad files are the source of truth once a board has been edited by hand.

Net names are PWR's. On the bank, GND is the stack's bottom plate and VCAP its top; LTC_GPI is the
wire to the LTC3350's GPI pin, which the bank's NTC pulls to the stack's bottom plate.
"""

PINS = {
    "1": "VCAP",  # stack + (power)
    "2": "VCAP",
    "3": "GND",  # stack - (power)
    "4": "GND",
    "5": "BANK_POS_S",  # stack + Kelvin sense
    "6": "BANK_NEG_S",  # stack - Kelvin sense
    "7": "BANK_TAP3",  # cell 3 / cell 4
    "8": "BANK_TAP2",  # cell 2 / cell 3
    "9": "BANK_TAP1",  # cell 1 / cell 2
    "10": "LTC_GPI",  # NTC, to stack - on the bank
}

HEADER = dict(
    lib_id="Connector_Generic:Conn_02x05_Odd_Even",
    fp="Connector_Molex:Molex_Micro-Fit_3.0_43045-1000_2x05_P3.00mm_Horizontal",
    mpn="43045-1000",
    mfr="Molex",
)

# The lead's own parts (qty, manufacturer, MPN, description), for each board's off-board BOM. Two pins per
# power pole: the peak is 7.25 A, and Molex rates even an 18 AWG circuit at only ~5.5 A in a fully loaded
# 12-way housing (PS-43045, 30 C rise); 20 AWG rates lower still.
LEAD = (
    (
        "2",
        "Molex",
        "43025-1000",
        "Micro-Fit 3.0 receptacle housing, 2 x 5, no panel ears; one per end",
    ),
    (
        "20",
        "Molex",
        "43030-0007",
        "Micro-Fit 3.0 female crimp, 20-24 AWG, tin to match the headers, bagged; one per circuit at each end",
    ),
    (
        "10",
        "",
        "",
        "Stranded wire, 20 AWG, insulation <= 1.85 mm OD (the crimp's limit), <= 300 mm; pin n to pin n",
    ),
)
