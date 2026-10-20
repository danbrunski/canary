# ruff: noqa: E501
"""The cap bank's placement and copper, in board millimetres (y down), read by gen_pcb.py.

Two rows of two cells lie flat with their leads facing a vertical centre strip, and J1 sits at the strip's
foot with its mating face off the bottom edge. The string runs C1 (bottom left) up to C2, across the top
to C3 and down to C4 (bottom right), so both of its ends land beside J1: GND at C1's - lead next to J1's
GND pins, VCAP at C4's + lead next to its VCAP pins.

    C2 body ====]  TAP2  [==== C3 body
                 TAP1 | TAP3
    C1 body ====]   J1   [==== C4 body

Every inter-cell join is a pour on both layers (4 A, 7.25 A peak). J1's power pins sit on its front row,
so VCAP and GND reach them round the row's ends and under the housing. The taps and the NTC carry
little current and drop onto J1's back row from the strip. The two sense lines cross, because the lead
puts + sense on pin 5 (front row, GND end) and - sense on pin 6 (back row, VCAP end). They pass each
other on opposite layers in the 1.5 mm channel between J1's rows: - sense on F.Cu, + sense on B.Cu.

Each sense line's entry into the channel cuts its own layer's power path to J1. On F.Cu, GND reaches J1
only the long way round NT2, and on B.Cu, VCAP only round the + sense via. The other layer carries those
two currents directly. So that no stack current flows past a sense point, each net tie sits in a pour
keep-out and joins its cell's pad by a trace of its own.
"""

BOARD = (106.5, 94.0, 193.5, 149.5)  # x0, y0, x1, y1
CORNER_R = 3.0

# Cable-tie slots, 3.6 x 1.6 mm: a tie round each column of two cells, near each end of the bodies.
TIE_X = (116.0, 132.0, 168.0, 184.0)
SLOTS = [(x - 1.8, y - 0.8, x + 1.8, y + 0.8) for x in TIE_X for y in (101.0, 140.0)]
HOLES = [(110.5, 98.0), (189.5, 98.0), (110.5, 145.5), (189.5, 145.5)]

TEXT = [
    ("CANary cap bank rev 1.0", 150.0, 96.6, "F", 1.5),
    ("DISCHARGE BEFORE UNPLUGGING", 150.0, 99.3, "F", 1.0),
    ("J1 to CANary-PWR J4: gold Micro-Fit 2x5 both ends, pin n to pin n", 150.0, 119.2, "B", 1.0),
    ("Discharge first: vshunt low until the red LED is out", 150.0, 121.4, "B", 1.0),
]
SILK = []

# ref: (x, y, rotation, side). A cell's origin is its + pad; at rotation 0 its - pad is 7.5 mm below and
# the body runs left, at 180 the - pad is above and the body runs right.
PLACEMENT = {
    "C1": (143.0, 126.25, 0, "F"),
    "C2": (143.0, 107.25, 0, "F"),
    "C3": (157.0, 114.75, 180, "F"),
    "C4": (157.0, 133.75, 180, "F"),
    "J1": (156.0, 140.0, 180, "F"),  # front row y 140 (pins 1-5 from x 156 left), back row y 137
    "TH1": (145.8, 131.5, 90, "F"),  # on cell 1's - lead copper; GPI pad down, GND pad up
    "NT1": (160.2, 133.75, 180, "F"),  # beside C4's + pad; VCAP pad toward it
    "NT2": (140.6, 131.4, 90, "F"),  # above-left of C1's - pad; GND pad up
}


def _both(name, pts):
    return [(name, side, pts, 0) for side in ("F", "B")]


POURS = [
    *_both("BANK_TAP2", [(138.0, 102.75), (162.0, 102.75), (162.0, 110.5), (138.0, 110.5)]),
    *_both("BANK_TAP1", [(138.0, 112.0), (148.5, 112.0), (148.5, 128.5), (138.0, 128.5)]),
    *_both("BANK_TAP3", [(151.5, 112.0), (162.0, 112.0), (162.0, 128.5), (151.5, 128.5)]),
    # Split under J1 between pin 3 (GND, x 150) and pin 2 (VCAP, x 153).
    *_both("GND", [(107.0, 129.0), (151.2, 129.0), (151.2, 149.0), (107.0, 149.0)]),
    *_both("VCAP", [(151.8, 129.0), (193.0, 129.0), (193.0, 149.0), (151.8, 149.0)]),
]

# (net, side, width, points). The taps start inside their own pours.
TRACKS = [
    ("BANK_TAP1", "F", 0.5, [(147.0, 127.5), (147.0, 137.0)]),
    ("BANK_TAP2", "F", 0.5, [(150.0, 109.5), (150.0, 137.0)]),
    ("BANK_TAP3", "F", 0.5, [(153.0, 127.5), (153.0, 137.0)]),
    ("LTC_GPI", "F", 0.3, [(145.8, 132.325), (145.8, 135.4), (144.0, 137.0)]),
    # Kelvin joins: each net tie to its cell's pad, inside the keep-outs below.
    ("GND", "F", 0.3, [(140.6, 130.9), (143.0, 130.9), (143.0, 132.6)]),
    ("VCAP", "F", 0.3, [(159.7, 133.75), (157.0, 133.75)]),
    # - sense: NT2 down past C1's - pad, through the channel to pin 6.
    ("BANK_NEG_S", "F", 0.3, [(140.6, 131.9), (140.6, 138.5), (156.0, 138.5), (156.0, 137.0)]),
    # + sense: NT1 to a via, then B.Cu through the channel to pin 5.
    ("BANK_POS_S", "F", 0.3, [(160.7, 133.75), (161.9, 133.75)]),
    (
        "BANK_POS_S",
        "B",
        0.3,
        [(161.9, 133.75), (161.9, 136.0), (159.4, 138.5), (144.0, 138.5), (144.0, 140.0)],
    ),
]
VIAS = [("BANK_POS_S", 161.9, 133.75, 0.6, 0.3)]

# Pour keep-outs (side, outline): the net ties and their Kelvin joins carry no pour, so the only copper
# between a sense point and its cell is the join trace.
KEEPOUTS = [
    (
        "F",
        [
            (139.95, 130.25),
            (143.45, 130.25),
            (143.45, 132.0),
            (142.55, 132.0),
            (142.55, 131.45),
            (139.95, 131.45),
        ],
    ),
    ("F", [(158.75, 133.15), (162.5, 133.15), (162.5, 134.35), (158.75, 134.35)]),
]
