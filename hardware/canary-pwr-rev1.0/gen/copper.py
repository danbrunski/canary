# ruff: noqa: E501
"""CANary-PWR rev 1.0 power copper (2026-10-15): pours, Kelvin sense pairs and stitching vias, drawn by hand.

gen_pcb.py adds these after placement. Every pour is a union of rectangles (x0, y0, x1, y1) in board mm on one
layer, filled as a zone that joins pads solidly and sits above the GND pours. The autorouter never routes the
current paths: hardware/tools/dsn_prep.py hands it each pour as fixed copper, so it only adds the low-current taps
(dividers, decoupling caps, IC supply pins) at a narrow width, plus the signals. Kelvin pairs leave each shunt at
the inner edge of its pads and stay clear of the pours. Nets are design names (gen_sch.kicad_net_names maps them).
Design record: docs/CANary-PWR-Spec.md §P2 (Layers) and §P10 item 15.
"""

Rect = tuple[float, float, float, float]

# (net, layer, rectangles). Gaps between different nets' rectangles are >= 0.3 mm, the zone clearance; where a
# rectangle overlaps another net's pad, the fill steps around it by the same clearance.
POURS: list[tuple[str, str, list[Rect]]] = [
    # Vehicle in: J2.1 to the reverse-protection FET's drain tab.
    ("+12V_RAW", "F", [(144.3, 80.4, 150.7, 88.9), (141.2, 87.1, 144.6, 93.6)]),
    # Protected input: Q1 source, C1 and the TVS, a band over Q2 and RSNSI to a finger down Q2's source pins,
    # with C4 (LTC3350 VIN) and the PFI divider top.
    (
        "+12V_PROT",
        "F",
        [
            (143.85, 68.5, 148.85, 79.9),
            (135.55, 68.5, 148.85, 69.98),
            (136.1, 67.0, 137.55, 68.6),
            (135.55, 68.5, 136.55, 72.1),
            (134.2, 70.95, 135.6, 72.0),
            (139.4, 78.45, 143.85, 82.4),
        ],
    ),
    # Input ideal diode's drain to RSNSI; beside the shunt it stops at the pad so the Kelvin pickup sees no current.
    ("LTC_VOUTSP", "F", [(136.85, 70.3, 139.9, 73.4), (139.6, 70.3, 143.5, 72.16)]),
    (
        "+12V_BUS",
        "F",
        [
            # Buck input: C26/C28 down to VIN1, C29/C27 up to VIN2, both necks between the GND and EN/CBOOT pins.
            (135.0, 51.3, 137.4, 56.25),
            (135.3, 56.0, 136.19, 56.75),
            (134.97, 60.75, 137.6, 65.75),
            (135.3, 60.25, 136.19, 61.0),
            # Bands to the bulk electrolytics, and the EN pull-up.
            (137.4, 51.3, 150.0, 53.75),
            (144.3, 53.5, 145.3, 54.5),
            (149.9, 51.3, 154.3, 65.75),
            (137.3, 64.45, 150.0, 66.0),
            # Charger: Q3 drain, C21, C20, and the band from RSNSI's bus end.
            (127.62, 77.73, 135.5, 81.15),
            (135.2, 77.3, 140.2, 78.1),
            # Output ideal diode's drain tab.
            (113.6, 75.2, 118.2, 80.8),
        ],
    ),
    # HAT-out polyfuse input, under the charger.
    ("+12V_BUS", "B", [(130.2, 77.75, 134.6, 80.3), (130.2, 80.3, 132.0, 84.4)]),
    # LM61460 switch node: pin 10 to L2, as small as the path allows.
    ("LM_SW", "F", [(129.9, 57.3, 133.75, 59.85), (133.6, 58.15, 134.0, 58.85)]),
    # LTC3350 switch node: Q3 source, Q4 drain, L1, the boost cap.
    (
        "LTC_SW",
        "F",
        [(123.2, 76.8, 127.32, 85.3), (126.6, 82.3, 129.25, 85.3), (126.4, 76.2, 127.32, 77.5)],
    ),
    # L1 to RSNSC; clear of the shunt's body, where the Kelvin pickup leaves.
    ("LTC_ICAP", "F", [(118.5, 76.7, 120.8, 84.0), (120.6, 83.6, 122.3, 88.8)]),
    # Cap bank: J4 pins 1/2, C15/C16, RSNSC and the output ideal diode's source pins.
    (
        "VCAP",
        "F",
        [(108.4, 82.3, 111.2, 94.0), (108.4, 88.3, 117.55, 94.0), (114.4, 81.1, 116.15, 88.5)],
    ),
    # Buck output: L2, RSNSI's input end, and down the side of C30/C31's GND pads to their outputs.
    (
        "+5V_BUCK",
        "F",
        [(119.55, 52.3, 126.1, 58.15), (122.15, 58.0, 125.0, 62.37), (115.45, 61.1, 122.2, 62.85)],
    ),
    ("+5V_SHUNT", "F", [(108.82, 55.4, 113.6, 57.55)]),
    # Pi 5 V: Q7's drain tab, C34, and the vias to the In2 island that feeds J1 pins 2 and 4.
    ("+5V_PI", "F", [(104.3, 53.4, 107.0, 56.3), (107.0, 49.95, 112.3, 54.73)]),
]

# In2 islands (priority over In2's GND). The +12V_BUS island is placement.IN2_ZONE.
IN2_PI_5V = [(107.3, 45.2), (112.2, 45.2), (112.2, 55.0), (107.3, 55.0)]

# Kelvin pairs and gate drives: (net, layer, width, points). Vias are in VIAS.
KELVIN_W = 0.25
TRACKS: list[tuple[str, str, float, list[tuple[float, float]]]] = [
    # RSNSI (R14) to LTC3350 VOUTSP (pin 31) and VOUTSN (pin 30, +12V_BUS), as a pair on B.Cu.
    ("LTC_VOUTSP", "F", KELVIN_W, [(141.85, 72.16), (141.85, 72.75)]),
    (
        "LTC_VOUTSP",
        "B",
        KELVIN_W,
        [(141.85, 72.75), (141.85, 73.8), (141.2, 74.45), (131.5, 74.45)],
    ),
    ("LTC_VOUTSP", "F", KELVIN_W, [(131.5, 74.45), (130.75, 74.45)]),
    ("+12V_BUS", "F", KELVIN_W, [(141.85, 76.85), (141.85, 76.25)]),
    ("+12V_BUS", "B", KELVIN_W, [(141.85, 76.25), (141.85, 75.85), (141.2, 75.2), (132.2, 75.2)]),
    ("+12V_BUS", "F", KELVIN_W, [(132.2, 75.2), (131.95, 75.4), (130.25, 75.4), (130.25, 74.45)]),
    # R28 (INA226 shunt) to INA226 IN+ (pin 10) and IN- (pin 9): IN+ under the board, IN- beside the shunt.
    ("+5V_BUCK", "F", KELVIN_W, [(118.645, 55.9), (118.3, 56.25), (118.3, 57.9)]),
    ("+5V_BUCK", "B", KELVIN_W, [(118.3, 57.9), (117.6, 58.6), (111.75, 58.6), (111.6, 58.75)]),
    ("+5V_BUCK", "F", KELVIN_W, [(111.6, 58.75), (112.4, 58.95)]),
    (
        "+5V_SHUNT",
        "F",
        KELVIN_W,
        [
            (113.955, 55.9),
            (114.2, 56.15),
            (114.2, 57.85),
            (113.2, 57.85),
            (112.9, 58.15),
            (112.9, 58.95),
        ],
    ),
    # RSNSC (R15) to LTC3350 VCAP (pin 21) and ICAP (pin 22): vias beside U1, B.Cu down the free strip left of
    # the J3 pins, between the ballast resistors' pads, and up under R15's body to the inner edge of each pad.
    ("VCAP", "F", KELVIN_W, [(125.75, 74.45), (125.75, 74.95), (125.05, 75.65), (124.75, 75.65)]),
    (
        "VCAP",
        "B",
        KELVIN_W,
        [
            (124.75, 75.65),
            (124.0, 76.4),
            (124.0, 84.5),
            (123.5, 85.0),
            (116.7, 85.0),
            (116.7, 86.8),
        ],
    ),
    ("VCAP", "F", KELVIN_W, [(116.7, 86.8), (116.055, 86.8)]),
    ("LTC_ICAP", "F", KELVIN_W, [(126.25, 74.45), (126.25, 75.45), (125.65, 76.05)]),
    (
        "LTC_ICAP",
        "B",
        KELVIN_W,
        [
            (125.65, 76.05),
            (124.6, 77.1),
            (124.6, 88.9),
            (124.1, 89.4),
            (120.55, 89.4),
            (120.05, 88.9),
            (120.05, 86.8),
        ],
    ),
    ("LTC_ICAP", "F", KELVIN_W, [(120.05, 86.8), (120.745, 86.8)]),
    # Gate drives: Q3's gate is boxed in by the switch-node and bus pours, and BG has no room for a via until it is
    # under the board.
    ("LTC_TG", "F", 0.3, [(127.75, 74.45), (127.75, 75.6)]),
    ("LTC_TG", "B", 0.3, [(127.75, 75.6), (127.0, 76.35), (127.0, 81.55)]),
    ("LTC_TG", "F", 0.3, [(127.0, 81.55), (127.0, 80.67)]),
    ("LTC_BG", "F", 0.3, [(128.75, 74.45), (128.75, 75.6)]),
    ("LTC_BG", "B", 0.3, [(128.75, 75.6), (128.75, 79.7), (130.0, 80.95), (130.0, 81.9)]),
    ("LTC_BG", "F", 0.3, [(130.0, 81.9), (129.88, 82.83)]),
]

SIGNAL_VIA = (0.6, 0.3)
POWER_VIA = (0.8, 0.4)
# Same-net vias stay >= 0.25 mm apart: Freerouting counts closer ones as violations.
VIAS: list[tuple[str, float, float, tuple[float, float]]] = [
    # Kelvin pairs.
    ("LTC_VOUTSP", 141.85, 72.75, SIGNAL_VIA),
    ("LTC_VOUTSP", 131.5, 74.45, SIGNAL_VIA),
    ("+12V_BUS", 141.85, 76.25, SIGNAL_VIA),
    ("+12V_BUS", 132.2, 75.2, SIGNAL_VIA),
    ("+5V_BUCK", 118.3, 57.9, SIGNAL_VIA),
    ("+5V_BUCK", 111.6, 58.75, SIGNAL_VIA),
    ("LTC_ICAP", 125.65, 76.05, SIGNAL_VIA),
    ("LTC_ICAP", 120.05, 86.8, SIGNAL_VIA),
    ("VCAP", 124.75, 75.65, SIGNAL_VIA),
    ("VCAP", 116.7, 86.8, SIGNAL_VIA),
    # Gate drives: TG and BG drop under the board beside U1 and come up at their FET's gate.
    ("LTC_TG", 127.75, 75.6, SIGNAL_VIA),
    ("LTC_TG", 127.0, 81.55, SIGNAL_VIA),
    ("LTC_BG", 128.75, 75.6, SIGNAL_VIA),
    ("LTC_BG", 130.0, 81.9, SIGNAL_VIA),
    # +12V_BUS pours to the In2 island (and the B.Cu pour under the charger).
    *[("+12V_BUS", x, 78.3, POWER_VIA) for x in (130.7, 131.85, 133.0, 134.15)],
    ("+12V_BUS", 138.6, 77.7, POWER_VIA),
    ("+12V_BUS", 139.65, 77.7, POWER_VIA),
    *[("+12V_BUS", x, 52.5, POWER_VIA) for x in (137.9, 139.1, 140.3, 141.5)],
    *[("+12V_BUS", x, y, POWER_VIA) for x in (151.0, 152.5) for y in (59.5, 61.5)],
    *[("+12V_BUS", x, 65.2, POWER_VIA) for x in (139.5, 141.0, 142.5)],
    *[("+12V_BUS", x, y, SIGNAL_VIA) for x in (116.8, 117.7) for y in (76.2, 77.3)],
    # +5V_PI pour to the In2 island under J1 pins 2 and 4.
    *[("+5V_PI", x, 51.0, POWER_VIA) for x in (107.8, 108.9, 110.0, 111.1)],
    # LM61460 input loop returns: beside GND pin 9 and C29/C27.
    ("GND", 134.4, 56.35, SIGNAL_VIA),
    ("GND", 133.5, 62.45, SIGNAL_VIA),
]
