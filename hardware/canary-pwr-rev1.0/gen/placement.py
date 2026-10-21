# ruff: noqa: E501
"""CANary-PWR rev 1.0 functional placement (2026-10-15): ref -> (x mm, y mm, rotation deg, side).

gen_pcb.py packs every part first and then moves each part listed here, so a regenerated board reproduces
this placement; a part added to the schematic later is packed until it gets a line here. Board
coordinates are KiCad's: x right from 100 (left edge), y down from 44 (header edge). A bottom-side
rotation is the value KiCad shows for the flipped footprint. Design record: docs/CANary-PWR-Spec.md §P2.
"""

PLACEMENT: dict[str, tuple[float, float, int, str]] = {
    # Edges: far edge (y = 100) LEDs, HAT out, Vehicle in; left edge (x = 100) button, SWD, cap bank. Fiducials.
    "J5": (107.305, 68.465, 180, "F"),
    "D7": (109.5, 98.82, 180, "F"),
    "D8": (113.0, 98.82, 180, "F"),
    "J3": (132.42, 90.56, 180, "F"),
    "J2": (142.5, 91.46, 0, "F"),
    "SW1": (102.38, 53.4, -90, "F"),
    "J4": (109.44, 87.2, 90, "F"),
    "FID3": (102.0, 60.8, 0, "F"),
    "FID1": (161.5, 53.5, 0, "F"),
    "FID2": (161.5, 91.0, 0, "F"),
    # 5 V path, top: L2 -> C30/C31 -> R28 shunt -> Q7 ideal diode, drain up into the J1 pin field under pins 2/4
    "Q7": (110.0, 53.9, 90, "F"),
    "C34": (105.4, 53.95, 90, "F"),
    "C35": (105.4, 58.0, 90, "F"),
    "R28": (116.3, 55.9, 180, "F"),
    "U4": (109.0, 58.1, 0, "F"),
    "C33": (109.4, 61.6, 90, "F"),
    "U3": (113.4, 61.1, 90, "F"),
    "C31": (117.1, 60.5, -90, "F"),  # 5 V pads up, into the buck output pour
    "C30": (120.5, 60.5, -90, "F"),
    # LM61460 buck, top: SW faces L2 (left); VIN1/VIN2 input pairs straddled above and below; FB/RT/EN parts to the right
    "L2": (127.15, 55.8, 180, "F"),  # 0.15 mm east, clear of BUTTON's vias
    "U2": (136.0, 58.5, 180, "F"),
    "C28": (135.0, 55.35, 180, "F"),
    "C26": (135.0, 52.9, 180, "F"),
    "C29": (134.7, 61.65, 180, "F"),  # 0.3 mm west: room for the SW lane to C24
    "C27": (135.0, 64.1, 180, "F"),
    # CBOOT under the RBOOT/CBOOT pins, its SW end fed through the gap between VIN2 and RBOOT (datasheet 12.1)
    "C24": (137.7, 61.65, 180, "F"),
    "C22": (139.9, 59.3, 90, "F"),
    "R21": (139.9, 55.3, 90, "F"),
    "R23": (141.6, 55.3, 90, "F"),
    "R22": (143.2, 55.3, 90, "F"),
    "R24": (141.6, 59.3, 90, "F"),
    "C25": (143.2, 59.3, 90, "F"),
    "R25": (144.8, 55.3, -90, "F"),  # bus end up, into the bus band
    "R26": (146.4, 55.3, 90, "F"),
    "Q6": (146.0, 59.6, 0, "F"),
    "R27": (146.0, 62.6, 0, "F"),
    "C23": (140.0, 62.9, -90, "F"),  # BIAS decoupling, beside U2 pin 1
    # LTC3350, top, rotated -90: analog/I2C pins up, VIN/INFET right, power pins down, cell taps left
    "U1": (128.0, 72.0, -90, "F"),
    # Two rows above U1's top row, 2 mm clear of its pins so the escapes (copper.py) fan out beneath them. Signal
    # pads face U1; the OUTFB parts (R10, R9, C12) and the CAPFB parts (R8, R7) sit in columns that do not cross.
    "C11": (129.4, 65.9, 90, "F"),
    "R8": (127.8, 65.9, 90, "F"),
    "R10": (126.2, 65.9, 90, "F"),
    "R5": (124.6, 65.9, 90, "F"),
    "R6": (123.0, 65.9, 90, "F"),
    "R7": (129.4, 62.6, 90, "F"),
    "C12": (127.8, 62.6, -90, "F"),  # bus end up (the top-side bus tap), OUTFB end toward U1
    "R9": (126.2, 62.6, -90, "F"),
    "R13": (124.6, 62.6, 90, "F"),
    "C13": (123.0, 62.6, 90, "F"),
    # C6 turned so CFP (pin 17) and CFN (pin 18) reach it without crossing; C6 and C7 0.5 mm west to fit the CAP4
    # via between C6 and U1's left pins (2026-10-18)
    "C6": (122.4, 72.9, 270, "F"),
    "C7": (122.5, 75.25, 180, "F"),
    # U1's right column (PFO, PFI, VCC2P5 x2, VIN, INFET, VOUTM5 from the top) leaves in lanes: PFO and PFI turn up,
    # C8 and C4 sit above the VIN and INFET lanes, C5 below them
    "C8": (133.05, 70.85, 90, "F"),
    "C5": (133.8, 74.05, 0, "F"),
    "C4": (134.65, 70.6, 90, "F"),
    # 0.7 mm below U1's bottom row: room for the TG and BG gate vias and the RSNSI Kelvin vias
    "C14": (127.9, 76.95, 180, "F"),
    # C10 (DRVCC 100 nF) under U1 beside the INTVCC via, so BST runs straight from C14 to D4 along the row it
    # left: on top, BST and INTVCC had to cross (2026-10-18). D4 sits 0.3 mm east to clear the RSNSI Kelvin via.
    "D4": (135.3, 76.4, 0, "F"),
    "R12": (134.0, 67.7, 0, "F"),
    "R11": (137.6, 67.7, 0, "F"),
    "Q2": (137.65, 71.5, 0, "F"),
    "R14": (141.85, 74.5, -90, "F"),
    "C2": (138.6, 75.3, 90, "F"),  # clear of the bus band that joins RSNSI to the charger
    # Charger stage, top: Q3/Q4 stacked with the SW node toward L1 and C21/C20 bridging VBUS-GND; R15, Q5 and the VCAP caps at the cap-bank pins
    "Q3": (128.44, 79.7, 0, "F"),
    "Q4": (128.44, 83.8, 180, "F"),
    "C21": (131.25, 81.7, -90, "F"),
    "C20": (133.85, 81.7, -90, "F"),
    "L1": (121.9, 80.3, 180, "F"),
    "Q5": (115.75, 80.0, 90, "F"),
    "R15": (118.4, 86.8, 180, "F"),
    "C15": (117.0, 90.1, 0, "F"),
    "C16": (117.0, 92.6, 0, "F"),
    "C17": (120.6, 92.6, 90, "F"),
    # Bus bulk and input protection, top: Q1 tab toward Vehicle in, TVS and bulk at the protected node
    "C19": (154.9, 57.0, 0, "F"),
    "C18": (154.9, 64.6, 0, "F"),
    "C1": (149.0, 72.2, 0, "F"),
    "Q1": (147.5, 82.6, -90, "F"),
    "D2": (140.9, 83.0, -90, "F"),
    "D1": (152.5, 79.5, 90, "F"),
    "R1": (155.0, 79.5, 90, "F"),
    "D3": (153.3, 86.2, 0, "F"),
    # Bottom: I2C buffer and the Pi-crossing parts in a row under J1; C45 0.5 mm east of centre, clear of the
    # OR_GATE via
    "U8": (112.6, 54.4, 180, "B"),
    "C45": (113.1, 57.6, 0, "B"),
    "R34": (117.0, 54.4, 90, "B"),
    "R35": (118.6, 54.4, 90, "B"),
    "R36": (120.2, 54.4, 90, "B"),
    "R42": (121.8, 54.4, -90, "B"),  # turned end for end in the operator's hand routing (c2020a48)
    "R38": (123.4, 54.4, 90, "B"),
    "R39": (125.0, 54.4, 90, "B"),
    "R40": (126.6, 54.4, 90, "B"),
    "R41": (128.2, 54.4, 90, "B"),
    "Q8": (131.0, 54.4, 0, "B"),
    "FID4": (135.0, 52.3, 0, "B"),
    "FID6": (102.0, 60.8, 0, "B"),
    "FID5": (161.5, 91.0, 0, "B"),
    "R37": (146.5, 52.1, 0, "B"),
    # Bottom: STM32G031 with its decoupling, reset cap and VIN divider in a column on its left (C32 is the INA226's);
    # the always-on LDO stays east
    "U5": (118.5, 64.0, 0, "B"),  # under the empty top area left of U1, not under U1's analog rows
    "R3": (109.2, 64.9, 90, "B"),
    "R4": (109.2, 61.6, 90, "B"),
    "C3": (111.7, 61.4, 180, "B"),
    "C38": (111.7, 62.95, 180, "B"),
    "C36": (111.7, 64.5, 180, "B"),
    "C32": (111.7, 59.85, 180, "B"),
    "U6": (134.3, 63.4, -90, "B"),
    "C39": (134.3, 68.2, 0, "B"),
    "C40": (134.3, 58.9, 0, "B"),
    "C37": (112.2, 67.8, 90, "B"),
    "D5": (135.3, 71.0, -90, "B"),  # AON_IN ends up toward C39; supply ends down
    "D6": (133.0, 71.0, -90, "B"),
    "R30": (128.4, 70.4, 90, "B"),
    "R31": (130.0, 70.4, 90, "B"),
    "C9": (131.1, 76.7, 0, "B"),  # LTC3350 INTVCC bulk, clear of the gate vias
    "C10": (129.6, 73.9, 180, "B"),  # DRVCC 100 nF, under U1's pins 28/29
    # Bottom: CAN wake transceiver between the HAT-out and Vehicle-in CAN pins; HAT-out polyfuse
    "U7": (141.0, 84.6, -90, "B"),
    "C41": (146.5, 86.2, 90, "B"),
    "C42": (140.0, 79.9, 0, "B"),
    "C43": (143.6, 79.9, 0, "B"),
    # F1 stands vertical: Bourns' 6.5 mm land overran the 5.8 mm between the BG via and U7
    "F1": (133.3, 82.6, -90, "B"),
    # Bottom: LED resistors; the five cell-ballast resistors stacked beside the cap-bank sense pins
    "R32": (110.5, 95.5, 0, "B"),
    "R33": (114.0, 95.5, 0, "B"),
    "R20": (118.3, 74.0, 180, "B"),
    "R17": (118.3, 78.4, 180, "B"),
    "R18": (118.3, 82.8, 180, "B"),
    "R19": (118.3, 87.2, 180, "B"),
    "R16": (118.3, 91.6, 180, "B"),
    # The tap ladder, added after routing on what copper there was, every rung on the LTC side of the ballasts
    # and clear of J4's hand-soldered pins (moved 2026-10-20 from the gaps of its right column, 0.48 mm off
    # the pins). R43 bridges the CAPRTN and CAP1 vias under U1's corner; R44 stands on top on CAP2's diagonal,
    # its CAP1 end on a via dropped to CAP1's In2 track; R45 bridges CAP3 and CAP2 right of R20/R17; R46 sits
    # on the CAP3/CAP4 tracks right of R20; R47 ties CAPRTN to GND at the corner of its track above R20.
    "R43": (124.35, 69.8, -90, "B"),
    "R44": (120.15, 72.15, 90, "F"),
    "R45": (122.6, 76.3, 90, "B"),
    "R46": (122.68, 72.6, 90, "B"),
    "R47": (115.0, 71.15, 180, "B"),
}

# Bottom-silk board label: inside the fan-inlet keep-out, which bars parts but not silk.
LABEL_B = (149.5, 66.0)

# In2 is GND (gen_pcb.py) except this +12V_BUS island, the higher-priority zone: under the buck input, the
# bus electrolytics, RSNSI, the charger FETs, Q5 and the HAT-out fuse. Everything the bottom carries west of
# x 132 and above y 73 (MCU, I2C buffer, Pi crossings) then sits 0.1 mm above GND instead of above the bus.
# R9 and C12 (OUTFB divider top, feed-forward) are the only +12V_BUS pads outside it: a top trace feeds them.
IN2_ZONE = [
    (113.0, 73.0),
    (132.0, 73.0),
    (132.0, 52.0),
    (164.0, 52.0),
    (164.0, 79.0),
    (136.0, 79.0),
    (136.0, 85.0),
    (113.0, 85.0),
]
