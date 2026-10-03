"""The 2008 Nissan Xterra's (VQ40DE ECM) inputs to the shared derived signals.

Profile ``nissan-xterra-2008``. The names are ECM Mode 22 identifiers from
``data/dids.yaml``; they sat in :mod:`src.analysis.derived` until R05.4.
"""

from __future__ import annotations

from collections.abc import Mapping

from src.signals.vehicle_signals import DidView, InputGroups, LoadAxisChannel

#: Inputs to the shared formulas whose inputs are vehicle-specific
#: (:data:`src.signals.derived.VEHICLE_INPUT_SIGNALS`).
SIGNAL_INPUTS: Mapping[str, InputGroups] = {
    # Wideband A/F sensor voltage, bank 1 minus bank 2.
    "derived:AFR_BANK_BALANCE": (("did:ECM_AF_SENSOR_V_B1",), ("did:ECM_AF_SENSOR_V_B2",)),
    # Raw TPS sensor-1 voltage over the ECM's calculated throttle percent.
    "derived:TPS_VOLTAGE_PER_PCT": (("did:ECM_THROTTLE_POS_SENSOR_1",), ("obd:THROTTLE_POS",)),
}

#: Wideband A/F sensor voltage per bank (0.005 V/count), converted to AFR
#: through the profile's ``afr_calibration.yaml`` transfer.
AF_SENSOR_VOLTAGE: Mapping[int, str] = {1: "did:ECM_AF_SENSOR_V_B1", 2: "did:ECM_AF_SENSOR_V_B2"}

#: Upstream wide-range lambda per bank. This ECM reports the PID 0x13 sensor
#: layout (its 0x15 answers as B1S2 and 0x19 as B2S2), so bank 2 sensor 1 is
#: J1979 sensor 5, PID 0x28 (D03.34, 2026-09-26).
WIDEBAND_LAMBDA: Mapping[int, str] = {1: "obd:O2_WR_LAMBDA_S1", 2: "obd:O2_WR_LAMBDA_S5"}

#: Base fuel schedule in ms, the Target AFR table's load axis (P02.14, operator
#: decision 2026-10-02). DID 0x1208 (``did:ECM_AIR_CHARGE``) is BFS. It stays
#: in raw counts, because the deployed IF and LSTM were trained on those, so
#: the ms reading is this separate channel. Its scale is the 0x1208
#: ``dids.yaml`` evidence record named here (0.00049 ms per count), and
#: nothing restates it.
BASE_FUEL_SCHEDULE = "derived:BASE_FUEL_SCHEDULE"

DID_VIEWS: tuple[DidView, ...] = (
    DidView(
        channel=BASE_FUEL_SCHEDULE,
        source="did:ECM_AIR_CHARGE",
        scale_method="uprev_stream_definition_vs_displayed_csv",
    ),
)

#: The Target AFR table's load axis is base fuel schedule (Tp, ms). Calculated
#: load (``obd:ENGINE_LOAD``, %) is not this axis: it is BFS divided by a
#: per-rpm table, and it fed the lookup until P02.13.
LOAD_AXIS_CHANNELS: tuple[LoadAxisChannel, ...] = (
    LoadAxisChannel(
        axis="base_fuel_schedule",
        channel=BASE_FUEL_SCHEDULE,
        evidence=(
            "UpRev's own stream definition puts B-FUEL SCHDL at DID 0x1208, and raw x "
            "0.00049 reproduces its displayed ms on 405/405 rows "
            "(uprev-detailed_scheme.pcapng + UpRev_Log9-20-2026_6-53-59_PM.csv, "
            "2026-09-20). A same-reply identity, so it is free of acquisition skew. "
            "Accepted as the verified BFS by the operator 2026-10-02 (P02.14). "
            "Steady state only, as dids.yaml records: BFS trails MAF by ~0.15 s, and "
            "this ECM never answers Mode 01 rpm and a Mode 22 DID at the same "
            "instant, so through a transient the rpm x BFS pair is skewed"
        ),
    ),
)
