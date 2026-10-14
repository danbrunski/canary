# ruff: noqa: E501, N802, N806, E741
# Bootstrap generator for the first-pass capture. Once the KiCad files are edited by hand they are the source of truth; regenerating overwrites them.
"""CANary-PWR rev 1.0 -- netlist and part data (design record: docs/CANary-PWR-Spec.md).

Every part is (sheet, group, lib_id, value, {pin: net}, extra fields). A pin mapped to None
gets a no-connect flag. Values are first-pass, taken from each datasheet's design equations;
the Note field on a part says where a number came from when it is not obvious.
"""

PARTS = []
SHEETS = ["Input", "Holdup", "Pi_Supply", "Supervisor", "Pi_Interface"]
SHEET_TITLES = {
    "Input": "Vehicle input, protection, switched 12 V to the HAT and to the charger",
    "Holdup": "LTC3350 supercapacitor charger / backup controller and the cap-bank connector",
    "Pi_Supply": "LM61460 5.1 V buck, input-current monitor, ideal-diode output to the Pi header",
    "Supervisor": "Always-on STM32G031 supervisor, its 3.3 V LDO, wake-on-CAN transceiver, SWD, button, LEDs",
    "Pi_Interface": "Pi 5 header (stacking), I2C hot-swap buffer, crossing nets into the Pi's 3.3 V domain",
}

FP = {
    "0603": ("Resistor_SMD:R_0603_1608Metric", "Capacitor_SMD:C_0603_1608Metric"),
    "0805": ("Resistor_SMD:R_0805_2012Metric", "Capacitor_SMD:C_0805_2012Metric"),
    "1206": ("Resistor_SMD:R_1206_3216Metric", "Capacitor_SMD:C_1206_3216Metric"),
    "1210": ("Resistor_SMD:R_1210_3225Metric", "Capacitor_SMD:C_1210_3225Metric"),
    "2512": ("Resistor_SMD:R_2512_6332Metric", None),
}
CAP_MPN = {
    ("100n", "50V"): "CL10B104KB8NNNC",
    ("10n", "50V"): "CL10B103KB8NNNC",
    ("1n", "50V"): "GRM1885C1H102JA01D",
    ("1u", "50V"): "CL21B105KBFNNNE",
    ("4.7u", "50V"): "CL31B475KBHNNNE",
    ("10u", "50V"): "GRM32ER71H106KA12L",
    ("47u", "10V"): "GRM32ER61A476KE20L",
    ("22u", "25V"): "GRM31CR61E226KE15L",
    ("22p", "50V"): "GRM1885C1H220JA01D",
    ("120p", "50V"): "GRM1885C1H121JA01D",
    ("220n", "25V"): "CL10B224KA8NNNC",
    ("1u", "25V"): "CL10A105KA8NNNC",
    ("4.7u", "25V"): "CL21A475KAQNNNE",
    ("22u", "10V"): "GRM31CR61A226KE19L",
    ("2.2u", "10V"): "CL10A225KP8NNNC",
}


def _yageo(value):
    v = value.replace("R", ".").upper()
    if v.endswith("K"):
        code = v[:-1].replace(".", "K") + ("K" if "." not in v[:-1] else "")
    elif v.endswith("M"):
        code = v[:-1].replace(".", "M") + ("M" if "." not in v[:-1] else "")
    else:
        code = v.replace(".", "R") + ("R" if "." not in v else "")
    return f"RC0603FR-07{code}L"


def part(
    sheet,
    group,
    lib_id,
    value,
    pins,
    fp="",
    mpn="",
    mfr="",
    ds="",
    desc="",
    note="",
    rot=0,
    dnp=False,
    ref_prefix=None,
    ref=None,
):
    PARTS.append(
        dict(
            sheet=sheet,
            group=group,
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
            dnp=dnp,
            ref_prefix=ref_prefix,
            fixed_ref=ref,
        )
    )


def R(sheet, group, value, a, b, size="0603", mpn=None, note="", desc="", mfr=None):
    if mpn is None:
        mpn, mfr = (_yageo(value), "Yageo") if size == "0603" else ("", "")
    part(
        sheet,
        group,
        "Device:R",
        value,
        {"1": a, "2": b},
        fp=FP[size][0],
        mpn=mpn,
        mfr=mfr or "",
        desc=desc or f"Resistor {value} {size}",
        note=note,
        rot=90,
    )


def C(sheet, group, value, a, b, volt="50V", size="0603", mpn=None, note="", desc=""):
    if mpn is None:
        mpn = CAP_MPN.get((value, volt), "")
    part(
        sheet,
        group,
        "Device:C",
        value,
        {"1": a, "2": b},
        fp=FP[size][1],
        mpn=mpn,
        desc=desc or f"Capacitor {value} {volt} X7R/X5R {size}",
        note=note,
        rot=90,
    )


def CP(sheet, group, value, a, b, mpn="EEE-FK1H470P", note=""):
    part(
        sheet,
        group,
        "Device:C_Polarized",
        value,
        {"1": a, "2": b},
        fp="Capacitor_SMD:CP_Elec_6.3x7.7",
        mpn=mpn,
        mfr="Panasonic",
        desc=f"Aluminium electrolytic {value} 50V, 6.3x7.7 SMD",
        note=note,
        rot=90,
    )


def NFET(sheet, group, g, d, s, mpn="CSD18543Q3A", note=""):
    if mpn == "CSD18543Q3A":
        part(
            sheet,
            group,
            "Transistor_FET:CSD18543Q3A",
            "CSD18543Q3A",
            {"1": s, "2": s, "3": s, "4": g, "5": d},
            fp="Package_SON:VSON-8_3.3x3.3mm_P0.65mm_NexFET",
            mpn="CSD18543Q3A",
            mfr="Texas Instruments",
            ds="https://www.ti.com/lit/ds/symlink/csd18543q3a.pdf",
            desc="60V N-channel NexFET, 8.1mOhm @10V, logic-level, VSON-8 3.3x3.3",
            note=note,
        )
    else:
        part(
            sheet,
            group,
            f"Transistor_FET:{mpn}",
            mpn,
            {"1": g, "2": s, "3": d},
            fp="Package_TO_SOT_SMD:SOT-23",
            mpn=mpn,
            mfr="onsemi" if mpn == "BSS138" else "Alpha & Omega",
            desc="N-channel MOSFET, SOT-23",
            note=note,
        )


# ---------------------------------------------------------------- Input
S = "Input"
g = "Vehicle input (pluggable terminal)"
part(
    S,
    g,
    "Connector_Generic:Conn_01x04",
    "Vehicle in",
    {"1": "+12V_RAW", "2": "GND", "3": "CANH", "4": "CANL"},
    fp="Connector_Phoenix_MC:PhoenixContact_MC_1,5_4-G-3.81_1x04_P3.81mm_Horizontal",
    mpn="1803293",
    mfr="Phoenix Contact",
    desc="MC 1,5/4-G-3,81 pluggable header; mating plug MC 1,5/4-ST-3,81 (1803594)",
    note="OBD 16 = +12V_RAW (18 AWG), OBD 4 = GND (18 AWG), OBD 6/14 = CANH/CANL. Spec P3. Verify Phoenix order numbers before ordering.",
)
part(
    S,
    g,
    "Transistor_FET:SUD50P06-15",
    "SUD50P06-15",
    {"1": "Q_REV_G", "2": "+12V_RAW", "3": "+12V_PROT"},
    fp="Package_TO_SOT_SMD:TO-252-2",
    mpn="SUD50P06-15",
    mfr="Vishay",
    ds="https://www.vishay.com/docs/68940/sud50p06.pdf",
    desc="-60V P-channel MOSFET, reverse-polarity block",
    note="Carried from the HAT (S3.7.1): drain (tab) faces +12V_RAW, source faces +12V_PROT; the body diode conducts input->output.",
)
R(S, g, "100k", "Q_REV_G", "GND", note="Q_REV gate pull-down (HAT R10)")
part(
    S,
    g,
    "Device:D_Zener",
    "BZT52C15",
    {"1": "+12V_PROT", "2": "Q_REV_G"},
    fp="Diode_SMD:D_SOD-123",
    mpn="BZT52C15",
    mfr="Diodes Inc",
    desc="15V zener, Q_REV gate-source clamp (HAT D3)",
    rot=0,
)
part(
    S,
    g,
    "Device:D_Zener",
    "SMBJ24A",
    {"1": "+12V_PROT", "2": "GND"},
    fp="Diode_SMD:D_SMB",
    mpn="SMBJ24A",
    mfr="Littelfuse",
    desc="600W TVS, 24V standoff, 38.9V max clamp (HAT D1)",
    note="Every 50V capacitor rating on this board follows from this clamp.",
)
CP(S, g, "47u", "+12V_PROT", "GND", note="Bulk on the protected input (HAT C2)")
C(S, g, "100n", "+12V_PROT", "GND")
part(
    S,
    g,
    "Power_Protection:NUP2105L",
    "NUP2105LT1G",
    {"1": "CANH", "2": "CANL", "3": "GND"},
    fp="Package_TO_SOT_SMD:SOT-23",
    mpn="NUP2105LT1G",
    mfr="onsemi",
    desc="Dual-line CAN bus ESD protector, 24V standoff",
)

g = "HAT out (pluggable terminal, jumper to the HAT's J2)"
part(
    S,
    g,
    "Device:Polyfuse",
    "PTC 0.5A",
    {"1": "+12V_BUS", "2": "+12V_HAT"},
    fp="Fuse:Fuse_1812_4532Metric",
    desc="Resettable fuse, 1812, 0.5 A hold, rated >= 40 V (MPN to choose)",
    note="HAT-out comes off the backed-up bus, so the HAT's K-line transceiver and TMAP supply stay in spec through a crank dip (the LTC3350 holds the bus >= 9 V). Replaces the switched TPS1HB35 (trimmed 2026-10-13). The HAT's own V12_SENSE then reads the bus, not the battery; PWR measures the battery (VIN_SENSE, LTC3350 meas_vin).",
    rot=90,
)
part(
    S,
    g,
    "Connector_Generic:Conn_02x03_Top_Bottom",
    "HAT out",
    {"1": "+12V_HAT", "2": "GND", "3": "CANH", "4": "V_BCKP_AON", "5": "GND", "6": "CANL"},
    fp="Connector_Molex:Molex_Micro-Fit_3.0_43045-0600_2x03_P3.00mm_Horizontal",
    mpn="43045-0600",
    mfr="Molex",
    desc="Micro-Fit 3.0 2x3 right-angle header; mating 43025-0600 + 43030 crimps (18-24 AWG)",
    note="Changed 2026-10-13 from a 5-way Phoenix MC (21.6 mm) to save ~7 mm of edge: latching, keyed, and the same family and crimp tool as the cap-bank connector. Molex numbering, 1-3 on one row, 4-6 on the other: CANH (3) and CANL (6) share a column; 12 V (1) sits beside GND (2). Jumper to rev 2.1 J2: +12V_HAT -> pin 1 (+12V_RAW), GND -> pin 2, CANH/CANL -> the HAT's CAN pair; V_BCKP_AON unused on rev 2.1.",
)
R(
    S,
    g,
    "1k",
    "+3V3_AON",
    "V_BCKP_AON",
    note="Always-on GNSS backup supply for rev 2.2/3.0 (spec P6); limits a harness short",
)

g = "Battery sense"
R(S, g, "1M", "+12V_PROT", "VIN_SENSE", note="10 uA parked; full scale 25 V at the 3.3 V ADC")
R(S, g, "150k", "VIN_SENSE", "GND")
C(S, g, "100n", "VIN_SENSE", "GND")

# ---------------------------------------------------------------- Holdup
S = "Holdup"
g = "LTC3350 backup controller"
part(
    S,
    g,
    "LTC3350EUHF-PBF:LTC3350EUHF-PBF",
    "LTC3350EUHF#PBF",
    {
        "1": "PRIV_SCL",
        "2": "PRIV_SDA",
        "3": "LTC_SMBALERT",
        "4": None,
        "5": "LTC_VC",
        "6": "LTC_CAPFB",
        "7": "LTC_OUTFB",
        "8": "GND",
        "9": "LTC_RT",
        "10": "LTC_GPI",
        "11": "LTC_ITST",
        "12": "LTC_CAPRTN",
        "13": "LTC_CAP1",
        "14": "LTC_CAP2",
        "15": "LTC_CAP3",
        "16": "LTC_CAP4",
        "17": "LTC_CFP",
        "18": "LTC_CFN",
        "19": "LTC_VCAPP5",
        "20": "LTC_OUTFET",
        "21": "VCAP",
        "22": "LTC_ICAP",
        "23": "LTC_VCC2P5",
        "24": "LTC_SW",
        "25": "LTC_TG",
        "26": "LTC_BST",
        "27": "LTC_BG",
        "28": "LTC_INTVCC",
        "29": "LTC_INTVCC",
        "30": "+12V_BUS",
        "31": "LTC_VOUTSP",
        "32": "LTC_VOUTM5",
        "33": "LTC_INFET",
        "34": "+12V_PROT",
        "35": "LTC_VCC2P5",
        "36": "LTC_VCC2P5",
        "37": "LTC_PFI",
        "38": "LTC_PFO",
        "39": "GND",
    },
    fp="canary-pwr:QFN-38_UHF_LIT",
    mpn="LTC3350EUHF#PBF",
    mfr="Analog Devices",
    ds="https://www.analog.com/media/en/technical-documentation/data-sheets/LTC3350.pdf",
    desc="Supercapacitor backup controller, 1-4 cells, I2C monitor (0x09)",
    note="Values follow datasheet Application Circuit 2 (11-20 V in, 10 V backup). VIN straight from the protected input: the 4 mA quiescent draw (no shutdown pin) is accepted parked (2026-10-13), which also keeps the bank topped up so key-on needs no recharge. Bank discharge for unplugging is firmware: lower vshunt and the cell shunts bleed ~0.5 A each (datasheet). CAPGD left open: chrg_status over I2C carries it. EP and SGND both GND; route SGND to the stack's bottom plate.",
)
C(S, g, "100n", "+12V_PROT", "GND", note="VIN decoupling")
C(S, g, "1u", "LTC_VOUTM5", "LTC_VOUTSP", volt="25V", note="VOUTM5 is 5 V below VOUT; cap sees 5 V")
C(S, g, "100n", "LTC_CFP", "LTC_CFN", note="Charge-pump flying cap")
C(S, g, "100n", "LTC_VCAPP5", "VCAP", note="VCAPP5 to VCAP")
C(S, g, "1u", "LTC_VCC2P5", "GND", volt="25V")
C(S, g, "4.7u", "LTC_INTVCC", "GND", volt="25V", size="0805", note="INTVCC/DRVCC tied; >=2.2 uF")
C(S, g, "100n", "LTC_INTVCC", "GND", note="At the DRVCC pin")
C(
    S,
    g,
    "10n",
    "LTC_VC",
    "GND",
    note="VC compensation: 8.2-10 nF when backing up through the output ideal diode",
)
R(S, g, "71.5k", "LTC_RT", "GND", note="fSW = 53.5/RT(k) = 748 kHz")
R(
    S,
    g,
    "121R",
    "LTC_ITST",
    "GND",
    mpn="RC0603FR-07121RL",
    mfr="Yageo",
    note="Capacitance-test current; 121 Ohm is the datasheet minimum. Set ctl_cap_scale=1: C = RT/RTST x 3.36 uF/LSB = ~2 mF/LSB",
)
R(
    S,
    g,
    "732k",
    "VCAP",
    "LTC_CAPFB",
    note="VCAP = 1.2 V x (1 + 732/100) = 9.98 V full-scale DAC (4 x 2.5 V, spec P4)",
)
R(S, g, "100k", "LTC_CAPFB", "GND")
R(S, g, "649k", "+12V_BUS", "LTC_OUTFB", note="Backup VOUT = 1.2 V x (1 + 649/100) = 9.0 V")
R(S, g, "100k", "LTC_OUTFB", "GND")
C(S, g, "120p", "+12V_BUS", "LTC_OUTFB", note="Phase lead: RFBO1 x CFBO1 = 1/(2 pi 2 kHz)")
R(
    S,
    g,
    "665k",
    "+12V_PROT",
    "LTC_PFI",
    note="Power-fail threshold = 1.17 V x (1 + 665/100) = 8.95 V falling. PFO goes to the MCU, not the Pi: a crank dip must not shut the Pi down.",
)
R(S, g, "100k", "LTC_PFI", "GND")
R(
    S,
    g,
    "10k",
    "LTC_INTVCC",
    "LTC_GPI",
    mpn="RT0603BRD0710KL",
    mfr="Yageo",
    note="NTC bias, 0.1% low drift (datasheet). Thermistor is on the cap bank.",
)
C(S, g, "10n", "LTC_GPI", "GND")

g = "Power path and bidirectional converter"
NFET(
    S,
    g,
    "LTC_INFET",
    "LTC_VOUTSP",
    "+12V_PROT",
    note="Input ideal diode: source at VIN, drain at VOUTSP",
)
R(
    S,
    g,
    "8m",
    "LTC_VOUTSP",
    "+12V_BUS",
    size="2512",
    mpn="WSL2512R0080FEA",
    mfr="Vishay",
    note="RSNSI: input current limit 32 mV / 8 mOhm = 4 A, load plus charger",
)
NFET(S, g, "LTC_TG", "+12V_BUS", "LTC_SW", note="Top switch")
NFET(S, g, "LTC_BG", "LTC_SW", "GND", note="Bottom switch")
part(
    S,
    g,
    "Device:L",
    "4.7u",
    {"1": "LTC_SW", "2": "LTC_ICAP"},
    fp="Inductor_SMD:L_Coilcraft_XAL7070-XXX",
    mpn="XAL7070-472MEB",
    mfr="Coilcraft",
    desc="4.7 uH shielded power inductor",
    note="L = VIN(max)/(ICHG(max) fSW) = 16/(4 A x 0.75 MHz) = 5.3 uH; Isat must exceed 1.8 x ICHG = 7.2 A",
    rot=90,
)
R(
    S,
    g,
    "8m",
    "LTC_ICAP",
    "VCAP",
    size="2512",
    mpn="WSL2512R0080FEA",
    mfr="Vishay",
    note="RSNSC: charge and backup current 32 mV / 8 mOhm = 4 A (peak 7.25 A). ESR readout scales with it.",
)
NFET(
    S, g, "LTC_OUTFET", "+12V_BUS", "VCAP", note="Output ideal diode: source at VCAP, drain at VOUT"
)
part(
    S,
    g,
    "Diode:1N4148W",
    "1N4148W",
    {"1": "LTC_BST", "2": "LTC_INTVCC"},
    fp="Diode_SMD:D_SOD-123",
    mpn="1N4148W",
    mfr="Diodes Inc",
    desc="Bootstrap diode",
    note="Fast PN, not Schottky: the datasheet warns BST-SW pumps past 5.5 V with a Schottky when the step-up runs non-synchronously beside the output ideal diode.",
)
C(S, g, "220n", "LTC_BST", "LTC_SW", volt="25V", note="CB >= 20 x top-FET Ciss")
C(S, g, "22u", "VCAP", "GND", volt="25V", size="1206")
C(S, g, "22u", "VCAP", "GND", volt="25V", size="1206")
C(S, g, "100n", "VCAP", "GND")
CP(
    S,
    g,
    "47u",
    "+12V_BUS",
    "GND",
    note="Bulk on VOUT: 100 uF per 2 A of backup current (datasheet)",
)
CP(S, g, "47u", "+12V_BUS", "GND")
C(S, g, "10u", "+12V_BUS", "GND", size="1210")
C(S, g, "100n", "+12V_BUS", "GND")

g = "Cell shunts and the cap-bank connector"
R(
    S,
    g,
    "2.7R",
    "LTC_CAPRTN",
    "BANK_NEG_S",
    size="2512",
    mpn="CRCW25122R70FKEG",
    mfr="Vishay",
    note="Shunt ballast: 500 mA at VSHUNT 2.7 V; P = 3 Vsh^2/(16 R) = 0.5 W",
)
R(S, g, "2.7R", "LTC_CAP1", "BANK_TAP1", size="2512", mpn="CRCW25122R70FKEG", mfr="Vishay")
R(S, g, "2.7R", "LTC_CAP2", "BANK_TAP2", size="2512", mpn="CRCW25122R70FKEG", mfr="Vishay")
R(S, g, "2.7R", "LTC_CAP3", "BANK_TAP3", size="2512", mpn="CRCW25122R70FKEG", mfr="Vishay")
R(S, g, "2.7R", "LTC_CAP4", "BANK_POS_S", size="2512", mpn="CRCW25122R70FKEG", mfr="Vishay")
part(
    S,
    g,
    "Connector_Generic:Conn_02x05_Odd_Even",
    "Cap bank",
    {
        "1": "VCAP",
        "2": "VCAP",
        "3": "GND",
        "4": "GND",
        "5": "BANK_POS_S",
        "6": "BANK_NEG_S",
        "7": "BANK_TAP3",
        "8": "BANK_TAP2",
        "9": "BANK_TAP1",
        "10": "LTC_GPI",
    },
    fp="Connector_Molex:Molex_Micro-Fit_3.0_43045-1000_2x05_P3.00mm_Horizontal",
    mpn="43045-1000",
    mfr="Molex",
    desc="Micro-Fit 3.0 2x5 right-angle header; mating 43025-1000 + 43030 crimps",
    note="Spec said 7-way; 10-way adds Kelvin sense for the stack top/bottom (CAP4/CAPRTN measure across the lead otherwise) and doubles the power pins. Bank board: 4 x 25 F cells, NTC from pin 10 to stack minus.",
)

S = "Pi_Supply"
g = "LM61460 5.1 V buck"
part(
    S,
    g,
    "LM61460AFSQRJRRQ1:LM61460AFSQRJRRQ1",
    "LM61460AASQRJRRQ1",
    {
        "1": "+5V_BUCK",
        "2": "LM_VCC",
        "3": "GND",
        "4": "LM_FB",
        "5": "BUCK_PG",
        "6": "LM_RT",
        "7": "BUCK_EN",
        "8": "+12V_BUS",
        "9": "GND",
        "10": "LM_SW",
        "11": "GND",
        "12": "+12V_BUS",
        "13": "LM_CBOOT",
        "14": "LM_CBOOT",
    },
    fp="canary-pwr:IC_LM61460AFSQRJRRQ1",
    mpn="LM61460AASQRJRRQ1",
    mfr="Texas Instruments",
    ds="https://www.ti.com/lit/ds/symlink/lm61460-q1.pdf",
    desc="36V 6A synchronous buck, auto mode, spread spectrum (AAS variant; same pinout as the AFS symbol)",
    note="Datasheet Table 10-2 row 400 kHz / 5 V. EN is the Pi's power switch (spec P5). Footprint: SnapMagic RJR0014A, checked against TI's land pattern.",
)
R(S, g, "33.2k", "LM_RT", "GND", note="RT = (1/400 kHz - 3.3e-5) x 1.346e4 = 33.2k")
C(S, g, "1u", "LM_VCC", "GND", volt="25V")
C(S, g, "100n", "+5V_BUCK", "GND", note="BIAS decoupling")
C(S, g, "100n", "LM_CBOOT", "LM_SW", note="CBOOT")
part(
    S,
    g,
    "Device:L",
    "4.7u",
    {"1": "LM_SW", "2": "+5V_BUCK"},
    fp="Inductor_SMD:L_Coilcraft_XAL7070-XXX",
    mpn="XAL7070-472MEB",
    mfr="Coilcraft",
    desc="4.7 uH, same part as the LTC3350's",
    rot=90,
)
R(S, g, "100k", "+5V_BUCK", "LM_FB", note="RFBT 100k")
R(
    S,
    g,
    "24.3k",
    "LM_FB",
    "GND",
    note="RFBB: 1 V x (1 + 100/24.3) = 5.115 V at the buck; ~50 mV is lost in the shunt and ideal diode at 5 A",
)
C(S, g, "22p", "+5V_BUCK", "LM_FF", note="CFF 22 pF + RFF 1k (Table 10-2)")
R(S, g, "1k", "LM_FF", "LM_FB")
C(S, g, "10u", "+12V_BUS", "GND", size="1210", note="CIN at VIN1/PGND1")
C(S, g, "10u", "+12V_BUS", "GND", size="1210", note="CIN at VIN2/PGND2")
C(S, g, "100n", "+12V_BUS", "GND", note="HF at VIN1")
C(S, g, "100n", "+12V_BUS", "GND", note="HF at VIN2")
C(S, g, "47u", "+5V_BUCK", "GND", volt="10V", size="1210")
C(S, g, "47u", "+5V_BUCK", "GND", volt="10V", size="1210")
g = "Enable (default on) and power-good"
R(S, g, "100k", "+12V_BUS", "BUCK_EN", note="EN divider: on above ~5.9 V; EN = 2.55 V at 12 V")
R(S, g, "27k", "BUCK_EN", "GND")
NFET(
    S,
    g,
    "PI_5V_OFF",
    "BUCK_EN",
    "GND",
    mpn="BSS138",
    note="MCU high = Pi off. MCU in reset/bootloader/hung -> gate pulled low -> Pi stays powered (spec P7)",
)
R(S, g, "100k", "PI_5V_OFF", "GND")
g = "5 V current monitor and ideal-diode output"
R(
    S,
    g,
    "5m",
    "+5V_BUCK",
    "+5V_SHUNT",
    size="2512",
    mpn="WSL2512R0050FEA",
    mfr="Vishay",
    note="25 mV at 5 A into the INA226's 81.92 mV range",
)
part(
    S,
    g,
    "Sensor_Energy:INA226",
    "INA226AQDGSRQ1",
    {
        "1": "GND",
        "2": "GND",
        "3": None,
        "4": "PRIV_SDA",
        "5": "PRIV_SCL",
        "6": "+3V3_AON",
        "7": "GND",
        "8": "+5V_PI",
        "9": "+5V_SHUNT",
        "10": "+5V_BUCK",
    },
    fp="Package_SO:TSSOP-10_3x3mm_P0.5mm",
    mpn="INA226AQDGSRQ1",
    mfr="Texas Instruments",
    ds="https://www.ti.com/lit/ds/symlink/ina226-q1.pdf",
    desc="I2C current/voltage monitor, address 0x40",
    note="VBUS reads the Pi's 5 V after the ideal diode. Put it in power-down while parked (0.5 uA).",
)
C(S, g, "100n", "+3V3_AON", "GND")
part(
    S,
    g,
    "Power_Management:LM74700",
    "LM74700QDBVRQ1",
    {"1": "OR_VCAP", "2": "GND", "3": "+5V_SHUNT", "4": "+5V_PI", "5": "OR_GATE", "6": "+5V_SHUNT"},
    fp="Package_TO_SOT_SMD:SOT-23-6",
    mpn="LM74700QDBVRQ1",
    mfr="Texas Instruments",
    ds="https://www.ti.com/lit/ds/symlink/lm74700-q1.pdf",
    desc="Ideal-diode controller, EN tied to ANODE (always on)",
    note="HAT+ spec: a Power HAT+ must only source current and tolerate USB-C connected at the same time (spec P5 rule 1).",
)
C(
    S,
    g,
    "100n",
    "OR_VCAP",
    "+5V_SHUNT",
    volt="25V",
    mpn="CL10B104KA8NNNC",
    note="Charge-pump cap, VCAP to ANODE",
)
NFET(
    S,
    g,
    "OR_GATE",
    "+5V_PI",
    "+5V_SHUNT",
    note="Ideal-diode FET: source at the buck side, drain at the Pi",
)
C(S, g, "22u", "+5V_PI", "GND", volt="10V", size="1206")
C(S, g, "100n", "+5V_PI", "GND")

# ---------------------------------------------------------------- Supervisor
S = "Supervisor"
g = "STM32G031K8 supervisor"
part(
    S,
    g,
    "MCU_ST_STM32G0:STM32G031K8Tx",
    "STM32G031K8T6",
    {
        "1": "LTC_SMBALERT",
        "2": "BUCK_PG",
        "3": None,
        "4": "+3V3_AON",
        "5": "GND",
        "6": "MCU_NRST",
        "7": "VIN_SENSE",
        "8": None,
        "9": None,
        "10": None,
        "11": "PI_3V3_SENSE",
        "12": "BUTTON",
        "13": "LED_STATUS",
        "14": "LED_BANK",
        "15": "PI_5V_OFF",
        "16": None,
        "17": None,
        "18": "~{PWR_FAIL}",
        "19": None,
        "20": None,
        "21": None,
        "22": "PRIV_SCL",
        "23": "PRIV_SDA",
        "24": "SWDIO",
        "25": "SWCLK_BOOT0",
        "26": "CAN_EN",
        "27": "CAN_STB_N",
        "28": "CAN_RXD",
        "29": "CAN_FAULT_N",
        "30": "MCU_SCL",
        "31": "MCU_SDA",
        "32": "LTC_PFO",
    },
    fp="Package_QFP:LQFP-32_7x7mm_P0.8mm",
    mpn="STM32G031K8T6",
    mfr="STMicroelectronics",
    ds="https://www.st.com/resource/en/datasheet/stm32g031k8.pdf",
    desc="Cortex-M0+ 64 KB, LQFP-32",
    note="I2C1 PB6/PB7 = the Pi-facing bus, also the system bootloader's I2C1 (AN2606 Table 105, target 0x56). PA14 = SWCLK and BOOT0. Program option bytes once over SWD: nBOOT_SEL=0 (BOOT0 pin honoured), hardware IWDG on. Internal pull-ups replace external ones (trimmed 2026-10-13) on PB9 SMBALERT, PB8 PFO, PA5 BUTTON, PB4 CAN RXD (the wake signal) and PC14 BUCK_PG; drop the PC14 pull-up while parked, because PGOOD sits low with the buck off. PA8 drives GPIO6 open-drain. Spare pins: PA1-PA3, PB1, PB2, PC6, PC15.",
)
C(S, g, "100n", "+3V3_AON", "GND")
C(S, g, "4.7u", "+3V3_AON", "GND", volt="25V", size="0805")
C(S, g, "100n", "MCU_NRST", "GND", note="NRST filter")

g = "Always-on 3.3 V"
part(
    S,
    g,
    "Device:D_Schottky",
    "BAT46WJ",
    {"1": "AON_IN", "2": "+12V_PROT"},
    fp="Diode_SMD:D_SOD-323F",
    mpn="BAT46WJ,115",
    mfr="Nexperia",
    desc="100V Schottky, diode-OR leg from the input",
    note="100 V because the other leg can sit at 0 V while this one sees the 38.9 V clamp",
)
part(
    S,
    g,
    "Device:D_Schottky",
    "BAT46WJ",
    {"1": "AON_IN", "2": "+12V_BUS"},
    fp="Diode_SMD:D_SOD-323F",
    mpn="BAT46WJ,115",
    mfr="Nexperia",
    desc="100V Schottky, diode-OR leg from the backed-up bus",
    note="Keeps the supervisor alive on hold-up after the input is lost",
)
part(
    S,
    g,
    "canary-pwr:TPS7A1633-Q1",
    "TPS7A1633AQDGNRQ1",
    {
        "8": "AON_IN",
        "5": "AON_IN",
        "6": None,
        "1": "+3V3_AON",
        "3": None,
        "7": None,
        "2": "GND",
        "4": "GND",
        "9": "GND",
    },
    fp="canary-pwr:DGN0008C_TPS7A1633AQDGNRQ1_TEX",
    mpn="TPS7A1633AQDGNRQ1",
    mfr="Texas Instruments",
    ds="https://www.ti.com/lit/ds/symlink/tps7a16-q1.pdf",
    desc="60V 5uA-Iq LDO, 3.3V fixed",
    note="The always-on regulator sets the parked current (spec P7), not the MCU. FB/NC tied to GND as on the HAT's U9.",
)
C(S, g, "1u", "AON_IN", "GND", size="0805")
C(S, g, "4.7u", "+3V3_AON", "GND", volt="25V", size="0805")

g = "Wake-on-CAN (receive-only by wiring)"
part(
    S,
    g,
    "Interface_CAN_LIN:TCAN1043xDxQ1",
    "TCAN1043DRQ1",
    {
        "1": "+3V3_AON",
        "2": "GND",
        "3": "+5V_BUCK",
        "4": "CAN_RXD",
        "5": "+3V3_AON",
        "6": "CAN_EN",
        "7": None,
        "8": "CAN_FAULT_N",
        "9": "CAN_WAKE",
        "10": "+12V_PROT",
        "11": None,
        "12": "CANL",
        "13": "CANH",
        "14": "CAN_STB_N",
    },
    fp="Package_SO:SOIC-14_3.9x8.7mm_P1.27mm",
    mpn="TCAN1043DRQ1",
    mfr="Texas Instruments",
    ds="https://www.ti.com/lit/ds/symlink/tcan1043-q1.pdf",
    desc="CAN FD transceiver with sleep and wake",
    note="TXD tied to VIO = permanently recessive: physically incapable of transmitting (S13.3.5). VCC is off while parked, which puts it in sleep; a bus wake pattern raises INH and pulls RXD low (datasheet 8.4.6.1).",
)
C(S, g, "100n", "+12V_PROT", "GND")
C(S, g, "100n", "+5V_BUCK", "GND")
C(S, g, "100n", "+3V3_AON", "GND")
R(S, g, "10k", "CAN_WAKE", "GND", note="Unused local WAKE pulled to ground (datasheet 8.4.6.2)")

g = "Private I2C pull-ups"
R(S, g, "4.7k", "PRIV_SDA", "+3V3_AON")
R(S, g, "4.7k", "PRIV_SCL", "+3V3_AON")
g = "Bench button, status LEDs, SWD"
part(
    S,
    g,
    "Switch:SW_Push",
    "EVQ-P7A01P",
    {"1": "BUTTON", "2": "GND"},
    fp="Button_Switch_SMD:SW_SPST_EVQP7A",
    mpn="EVQ-P7A01P",
    mfr="Panasonic",
    desc="Side-actuated bench button on the board edge: wake / force-on; hold = discharge the bank (parked only)",
)
R(S, g, "1k", "LED_STATUS", "LED1_A")
part(
    S,
    g,
    "Device:LED",
    "Green",
    {"1": "GND", "2": "LED1_A"},
    fp="LED_SMD:LED_Kingbright_APA1606_1.6x0.6mm_Horizontal",
    mpn="APA1606SGC",
    mfr="Kingbright",
    desc="Side-view status LED on the board edge",
)
R(S, g, "1k", "LED_BANK", "LED2_A")
part(
    S,
    g,
    "Device:LED",
    "Red",
    {"1": "GND", "2": "LED2_A"},
    fp="LED_SMD:LED_Kingbright_APA1606_1.6x0.6mm_Horizontal",
    mpn="APA1606SURCK",
    mfr="Kingbright",
    desc="'Bank live' LED: lit only while the button is held or discharge runs",
)
part(
    S,
    g,
    "Connector:Conn_ARM_JTAG_SWD_10",
    "SWD",
    {
        "1": "+3V3_AON",
        "2": "SWDIO",
        "3": "GND",
        "4": "SWCLK_BOOT0",
        "5": "GND",
        "6": None,
        "7": None,
        "8": None,
        "9": "GND",
        "10": "MCU_NRST",
    },
    fp="Connector_PinHeader_1.27mm:PinHeader_2x05_P1.27mm_Horizontal",
    mpn="",
    mfr="",
    desc="Cortex 10-pin SWD, right-angle on the board edge, bench only (MPN to choose)",
    note="Edge-mounted so a probe reaches it from the side of the stack. Field reflash goes over I2C instead.",
)

# ---------------------------------------------------------------- Pi_Interface
S = "Pi_Interface"
g = "Pi 5 header (stacking, the HAT plugs on top)"
j1 = {str(n): None for n in range(1, 41)}
j1.update(
    {
        "1": "PI_3V3",
        "17": "PI_3V3",
        "2": "+5V_PI",
        "4": "+5V_PI",
        "3": "PI_SDA",
        "5": "PI_SCL",
        "13": "PI_BOOT0",
        "18": "PI_NRST",
        "31": "~{PWR_FAIL}",
    }
)
for n in (6, 9, 14, 20, 25, 30, 34, 39):
    j1[str(n)] = "GND"
part(
    S,
    g,
    "Connector_Generic:Conn_02x20_Odd_Even",
    "2x20 stacking hdr 13.5mm (DNP)",
    j1,
    fp="Connector_PinSocket_2.54mm:PinSocket_2x20_P2.54mm_Vertical",
    dnp=True,
    ref="J1",
    desc="Pi 40-pin stacking header, 13.5 mm body, on B.Cu",
    note="Operator-sourced and fitted, as on HAT rev 2.1 (S13.5.6). Every pin passes through to the HAT; PWR touches only the ones with nets. GPIO27 (pin 13) = MCU BOOT0, GPIO24 (pin 18) = MCU reset, GPIO6 (pin 31) = power fail. GPIO24 is free on rev 2.1 (PWR supports rev 2.1 and later).",
)
C(S, g, "100n", "+5V_PI", "GND")

g = "I2C hot-swap buffer (Pi side powered)"
part(
    S,
    g,
    "canary-pwr:TCA4307",
    "TCA4307DGKR",
    {
        "6": "PI_SDA",
        "3": "PI_SCL",
        "1": "PI_3V3",
        "7": "MCU_SDA",
        "2": "MCU_SCL",
        "5": "I2C_READY",
        "8": "PI_3V3",
        "4": "GND",
    },
    fp="canary-pwr:SOIC8_DGK_TEX",
    mpn="TCA4307DGKR",
    mfr="Texas Instruments",
    ds="https://www.ti.com/lit/ds/symlink/tca4307.pdf",
    desc="I2C buffer, powered from the Pi's 3.3 V",
    note="Powered-off high-impedance I/O (datasheet p.1): the always-on board can never load or back-feed the Pi's I2C1 while the Pi is dark (spec P6). Pi side uses the Pi's own 1.8k pull-ups.",
)
C(S, g, "100n", "PI_3V3", "GND")
R(
    S,
    g,
    "10k",
    "MCU_SDA",
    "PI_3V3",
    note="Card-side pull-ups to the Pi's 3.3 V, so nothing pulls up while the Pi is off",
)
R(S, g, "10k", "MCU_SCL", "PI_3V3")
R(S, g, "10k", "I2C_READY", "PI_3V3")

g = "Crossing nets (safe-state pulls)"
R(
    S,
    g,
    "10k",
    "~{PWR_FAIL}",
    "PI_3V3",
    note="Pull-up to the Pi's rail: no back-feed while the Pi is off; a dead MCU reads as power OK",
)
R(S, g, "1k", "PI_BOOT0", "SWCLK_BOOT0")
R(S, g, "10k", "SWCLK_BOOT0", "GND", note="BOOT0 low unless the Pi holds GPIO27 high")
R(S, g, "1k", "PI_NRST", "NRST_GATE")
R(S, g, "100k", "NRST_GATE", "GND")
NFET(
    S,
    g,
    "NRST_GATE",
    "MCU_NRST",
    "GND",
    mpn="BSS138",
    note="A FET, not a resistor: a powered-off Pi pin would otherwise clamp NRST and hold the supervisor in reset",
)
R(S, g, "10k", "PI_3V3", "PI_3V3_SENSE", note="Halt detection: Pi 5 halt = 3V3 down")
