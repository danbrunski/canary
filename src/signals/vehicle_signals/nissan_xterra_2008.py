"""The 2008 Nissan Xterra's (VQ40DE ECM) inputs to the shared derived signals.

Profile ``nissan-xterra-2008``. The names are ECM Mode 22 identifiers from
``data/dids.yaml``; they sat in :mod:`src.analysis.derived` until R05.4.
"""

from __future__ import annotations

from collections.abc import Mapping

from src.signals.vehicle_signals import InputGroups

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
