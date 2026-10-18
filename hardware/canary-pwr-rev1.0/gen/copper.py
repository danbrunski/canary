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
            (134.97, 60.75, 135.85, 62.6),
            (134.97, 62.45, 137.6, 65.75),
            (135.3, 60.25, 135.75, 61.0),
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
    # Buck output: L2, the INA226 shunt's input end and C30/C31's output pads.
    (
        "+5V_BUCK",
        "F",
        [(119.55, 52.3, 126.1, 58.15), (115.45, 58.15, 122.2, 59.9)],
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
    # Pin 30 turns east at once, leaving room beside pin 29 for the INTVCC via.
    (
        "+12V_BUS",
        "F",
        KELVIN_W,
        [(132.2, 75.2), (131.95, 75.4), (130.75, 75.4), (130.25, 74.9), (130.25, 74.45)],
    ),
    # R28 (INA226 shunt) to INA226 IN+ (pin 10) and IN- (pin 9): IN+ under the board, IN- beside the shunt.
    ("+5V_BUCK", "F", KELVIN_W, [(118.645, 55.9), (118.2, 56.3), (117.8, 56.3)]),
    (
        "+5V_BUCK",
        "B",
        KELVIN_W,
        [(117.8, 56.3), (117.8, 57.9), (117.1, 58.6), (112.0, 58.6), (111.75, 58.85)],
    ),
    ("+5V_BUCK", "F", KELVIN_W, [(111.75, 58.85), (112.4, 58.95)]),
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
    ("LTC_BG", "F", 0.3, [(128.75, 74.45), (128.75, 75.2), (129.0, 75.45), (129.0, 75.5)]),
    ("LTC_BG", "B", 0.3, [(129.0, 75.5), (128.75, 79.7), (130.0, 80.95), (130.0, 81.9)]),
    ("LTC_BG", "F", 0.3, [(130.0, 81.9), (129.88, 82.83)]),
    # Spread the BG via to leave a 0.2 mm bootstrap escape to C14 (2026-10-18).
    (
        "LTC_BST",
        "F",
        0.2,
        [(128.25, 74.4524), (128.25, 75.0), (128.36, 75.11), (128.36, 76.635), (128.675, 76.95)],
    ),
    # INTVCC/DRVCC (pins 28, 29): one via beside pin 29 feeds C10 and C9 underneath, and a top lane under the RSNSI
    # Kelvin pair feeds D4. BST then runs straight from C14 to D4 (2026-10-18).
    ("LTC_INTVCC", "F", 0.2, [(129.25, 74.452), (129.75, 74.452), (129.75, 75.4), (129.95, 75.6)]),
    (
        "LTC_INTVCC",
        "F",
        0.25,
        [
            (129.95, 75.6),
            (130.5, 76.15),
            (132.3, 76.15),
            (132.95, 75.5),
            (136.95, 75.5),
            (136.95, 76.4),
        ],
    ),
    ("LTC_INTVCC", "B", 0.25, [(129.95, 75.6), (130.375, 75.175), (130.375, 74.3)]),
    ("LTC_INTVCC", "B", 0.25, [(129.95, 75.6), (130.15, 76.3)]),
    ("LTC_BST", "F", 0.25, [(128.675, 76.95), (133.4, 76.95)]),
    # LM61460 bootstrap: SW leaves pin 10 under the device through the VIN2/RBOOT gap to C24, RBOOT is tied to
    # CBOOT at the pins, and CBOOT steps under pin 1 to C24 (datasheet 12.1).
    ("LM_SW", "F", 0.22, [(136.125, 58.5), (136.125, 60.75), (136.6, 61.225)]),
    (
        "LM_CBOOT",
        "F",
        0.2,
        [(136.625, 60.1), (137.125, 60.1), (137.125, 60.8), (138.3, 60.8), (138.3, 61.3)],
    ),
    # LTC3350 VCC2P5: pin 23 has no way out between ICAP, SW and the TG via, so it runs inside U1's courtyard, in
    # the 0.43 mm between the exposed pad and the pin rows, to pins 35/36 and C8 (0.15 mm, 0.13 mm clearance by the
    # U1 land pattern rule).
    (
        "LTC_VCC2P5",
        "F",
        0.15,
        [(126.75, 74.2), (126.75, 73.84), (130.84, 73.84), (130.84, 72.0), (131.2, 72.0)],
    ),
    # LTC3350 VIN (pin 34) east under C8 to the +12V_PROT pour at C4.
    ("+12V_PROT", "F", 0.25, [(131.45, 72.5), (134.35, 72.5), (134.6, 72.25), (134.6, 71.9)]),
    # CAP4 (pin 16) drops through a via between C6 and the pin column, then B.Cu to R20; CFP runs under the via.
    ("LTC_CAP4", "F", 0.2, [(124.55, 72.0), (123.5, 72.05)]),
    ("LTC_CAP4", "B", 0.25, [(123.5, 72.05), (121.75, 72.05), (121.3, 72.5)]),
    ("LTC_CFP", "F", 0.2, [(124.4, 72.5), (124.3, 72.6), (122.6, 72.6)]),
    # LM61460 PGND (pin 11) straight down to C29's GND pad, the VIN2 input loop, and on to C29's via (datasheet
    # 12.1); before the bus tap moved, routed tracks left pin 11 alone in a scrap of pour.
    ("GND", "F", 0.3, [(134.2, 60.3), (134.2, 61.4), (133.9, 61.6), (132.64, 61.48)]),
    # Other IC GND pins tied to their own vias, so a routed track cannot leave them in a scrap of pour: INA226
    # (pin 7 under the body, boxed in by the IN- Kelvin trace) and LM61460 AGND.
    ("GND", "F", 0.2, [(113.9, 59.5), (113.9, 60.5)]),
    ("GND", "F", 0.2, [(137.85, 58.5), (138.65, 58.5)]),
    # LM74700 ANODE (pin 3) under the body to the shunt-side pour, and its GATE (pin 5) to Q7 through vias and
    # In2: a gate trace on top would cut the 5 V path, which runs from R28 to Q7's sources past the gate pin.
    ("+5V_SHUNT", "F", 0.25, [(107.86, 59.05), (108.6, 59.05), (109.0, 58.65), (109.0, 57.3)]),
    ("OR_GATE", "F", 0.2, [(110.97, 55.34), (111.95, 55.2)]),
    ("OR_GATE", "In2", 0.15, [(111.95, 55.2), (111.95, 55.6), (111.3, 56.25), (111.3, 58.1)]),
    ("OR_GATE", "F", 0.2, [(110.14, 58.1), (111.3, 58.1)]),
]

# U1's top-row escapes (0.2 mm; 0.15 where the CAPFB lane passes a bus-class pad). The pins fan out in a
# staircase under the two rows: ITST, GPI, RT and OUTFB turn left at 0.4 mm steps, GPI climbs between R6 and R5
# to C13/R13, OUTFB drops between R10 and R8 to R9/C12, CAPFB climbs between R8 and C11 to R7. Pin 8 joins the
# exposed pad and drops through a via between RT and OUTFB, which leave their pins 0.1 mm apart to make room: the
# exposed pad's only other way to GND was the top pour, which routed tracks cut on three of six plugin runs.
# OUTFB reaches R9/C12 straight up, not round R8's GND pad, so R8 gets a via above it (2026-10-18).
ESC_W = 0.2
ESCAPES: list[tuple[str, str, float, list[tuple[float, float]]]] = [
    ("LTC_ITST", "F", ESC_W, [(125.75, 69.55), (125.75, 68.75), (123.0, 68.75), (123.0, 66.725)]),
    (
        "LTC_GPI",
        "F",
        ESC_W,
        [(126.25, 69.55), (126.25, 68.35), (123.8, 68.35), (123.8, 61.775), (124.6, 61.775)],
    ),
    ("LTC_GPI", "F", ESC_W, [(123.8, 63.375), (123.0, 63.375)]),
    ("LTC_RT", "F", ESC_W, [(126.65, 69.55), (126.65, 67.95), (124.6, 67.95), (124.6, 66.725)]),
    ("LTC_OUTFB", "F", ESC_W, [(127.85, 69.55), (127.85, 67.55), (126.2, 67.55), (126.2, 66.725)]),
    ("LTC_OUTFB", "F", ESC_W, [(127.0, 67.55), (127.0, 63.4)]),
    ("LTC_OUTFB", "F", ESC_W, [(126.2, 63.425), (127.0, 63.4), (127.8, 63.375)]),
    ("LTC_CAPFB", "F", ESC_W, [(128.25, 69.55), (128.25, 66.725), (128.6, 66.725)]),
    ("LTC_CAPFB", "F", 0.15, [(128.6, 66.725), (128.6, 61.775), (129.4, 61.775)]),
    ("GND", "F", ESC_W, [(127.25, 69.55), (127.25, 70.5)]),
    ("GND", "F", ESC_W, [(127.25, 69.55), (127.25, 68.6)]),
    ("GND", "F", 0.3, [(127.8, 64.28), (127.8, 65.08)]),
    # The bus tap feeds R9/C12 (OUTFB divider top, feed-forward) from the In2 +12V_BUS island under the board. A
    # top trace from the buck's input pour ran between U2's PGND pin 11 and C29's GND pad and cut them apart.
    ("+12V_BUS", "F", 0.3, [(126.2, 61.775), (126.2, 61.0), (127.8, 61.0), (127.8, 61.825)]),
    ("+12V_BUS", "B", 0.25, [(127.0, 61.0), (128.0, 62.0), (131.0, 62.0), (132.3, 63.3)]),
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
    ("+5V_BUCK", 117.8, 56.3, SIGNAL_VIA),  # under R28's body
    ("+5V_BUCK", 111.75, 58.85, SIGNAL_VIA),
    ("LTC_ICAP", 125.65, 76.05, SIGNAL_VIA),
    ("LTC_ICAP", 120.05, 86.8, SIGNAL_VIA),
    ("VCAP", 124.75, 75.65, SIGNAL_VIA),
    ("VCAP", 116.7, 86.8, SIGNAL_VIA),
    # Gate drives: TG and BG drop under the board beside U1 and come up at their FET's gate.
    ("LTC_TG", 127.75, 75.6, SIGNAL_VIA),
    ("LTC_TG", 127.0, 81.55, SIGNAL_VIA),
    ("LTC_BG", 129.0, 75.5, SIGNAL_VIA),
    ("LTC_BG", 130.0, 81.9, SIGNAL_VIA),
    ("LTC_INTVCC", 129.95, 75.6, SIGNAL_VIA),
    ("LTC_CAP4", 123.5, 72.05, SIGNAL_VIA),
    # R9/C12 bus tap: beside R9/C12, and in the In2 island.
    ("+12V_BUS", 127.0, 61.0, SIGNAL_VIA),
    ("+12V_BUS", 132.3, 63.3, SIGNAL_VIA),
    ("OR_GATE", 111.95, 55.2, SIGNAL_VIA),
    ("OR_GATE", 111.3, 58.1, SIGNAL_VIA),
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
    # LM61460 input loop returns: beside C28 and GND pin 9, and beside C27 (clear of every pad: no via-in-pad).
    ("GND", 133.35, 56.2, SIGNAL_VIA),
    ("GND", 132.5, 62.45, SIGNAL_VIA),
    # A GND via beside each passive's GND pad, so routed tracks cannot cut it off from In1: seven such pads
    # were left in outer-pour fragments on the 29-unconnected plugin run (2026-10-18). Spots found by search:
    # 0.15 mm from every pad (0.25 from power-high copper), outside the part and the +5V_PI island.
    ("GND", 139.9, 57.62, SIGNAL_VIA),  # C22.2
    ("GND", 125.41, 65.68, SIGNAL_VIA),  # R10.2
    ("GND", 119.52, 92.6, SIGNAL_VIA),  # C16.2
    ("GND", 133.99, 84.22, SIGNAL_VIA),  # C20.2
    ("GND", 132.64, 61.48, SIGNAL_VIA),  # C29.2
    ("GND", 132.48, 53.04, SIGNAL_VIA),  # C26.2
    ("GND", 147.35, 54.48, SIGNAL_VIA),  # R26.2
    ("GND", 157.6, 65.85, SIGNAL_VIA),  # C18.2
    ("GND", 105.26, 51.43, SIGNAL_VIA),  # C34.2
    ("GND", 120.64, 63.02, SIGNAL_VIA),  # C30.2
    ("GND", 132.11, 69.95, SIGNAL_VIA),  # C8.2
    ("GND", 151.7, 73.45, SIGNAL_VIA),  # C1.2
    ("GND", 119.66, 91.7, SIGNAL_VIA),  # C17.2
    ("GND", 132.48, 64.24, SIGNAL_VIA),  # C27.2
    ("GND", 130.35, 65.12, SIGNAL_VIA),  # C11.2
    ("GND", 106.35, 57.23, SIGNAL_VIA),  # C35.2
    ("GND", 117.24, 63.02, SIGNAL_VIA),  # C31.2
    ("GND", 119.52, 90.1, SIGNAL_VIA),  # C15.2
    ("GND", 134.27, 68.9, SIGNAL_VIA),  # C4.2
    ("GND", 147.72, 62.6, SIGNAL_VIA),  # R27.2
    ("GND", 154.77, 77.81, SIGNAL_VIA),  # R1.2
    ("GND", 123.88, 60.94, SIGNAL_VIA),  # C13.2
    ("GND", 142.39, 55.08, SIGNAL_VIA),  # R23.2
    ("GND", 137.82, 73.75, SIGNAL_VIA),  # C2.2
    ("GND", 140.95, 63.67, SIGNAL_VIA),  # C23.2
    ("GND", 134.7, 66.76, SIGNAL_VIA),  # R12.2
    ("GND", 138.96, 54.6, SIGNAL_VIA),  # R21.2
    ("GND", 132.36, 82.18, SIGNAL_VIA),  # C21.2
    ("GND", 157.6, 58.25, SIGNAL_VIA),  # C19.2
    ("GND", 112.2, 65.9, SIGNAL_VIA),  # C37.2
    ("GND", 111.7, 60.63, SIGNAL_VIA),  # C32.2
    ("GND", 135.57, 66.99, SIGNAL_VIA),  # C39.2
    ("GND", 110.01, 63.2, SIGNAL_VIA),  # C38.2
    ("GND", 127.72, 52.75, SIGNAL_VIA),  # R41.2
    ("GND", 111.05, 65.44, SIGNAL_VIA),  # C36.2
    ("GND", 108.26, 60.9, SIGNAL_VIA),  # R4.2
    ("GND", 114.88, 57.6, SIGNAL_VIA),  # C45.2
    # The same for the pads routed tracks cut off on the 2026-10-18 plugin runs (U1's exposed pad on three of six,
    # R8 on all six, U2 PGND, the INA226), and for the other IC GND pins with a spot beside them.
    ("GND", 127.25, 68.6, SIGNAL_VIA),  # U1.8 / U1.39, between RT and OUTFB
    ("GND", 127.8, 64.28, SIGNAL_VIA),  # R8.2, under C12
    ("GND", 113.9, 60.5, SIGNAL_VIA),  # U3.7, under the body
    ("GND", 138.65, 58.5, SIGNAL_VIA),  # U2.3
    ("GND", 106.75, 58.4, SIGNAL_VIA),  # U4.2
    ("GND", 137.92, 80.83, SIGNAL_VIA),  # U7.2
    ("GND", 145.06, 61.3, SIGNAL_VIA),  # Q6.2
    ("GND", 130.97, 52.75, SIGNAL_VIA),  # Q8.2
    ("GND", 155.44, 86.2, SIGNAL_VIA),  # D3.3
    ("GND", 111.8, 63.71, SIGNAL_VIA),  # U3.1
    ("GND", 114.95, 56.3, SIGNAL_VIA),  # U8.4, under R28's body
]
