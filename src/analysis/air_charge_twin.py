"""BFS predicted from independently observed inputs, and scored per sample (E05.2).

The kernel (:mod:`src.decode.air_charge`) reproduces ``0x03C62C`` and the Fuel
Comp lookup integer for integer. What it cannot be given from a capture is the
airflow long ``0xFFFF2100``: the MAF pipeline that makes it carries filter
state and an unidentified term, so the dynamic BFS stays conditional
(application model blocker ``airflow_unmodelled``). What *is* exact without it
is the steady state:

* the filter's fixed point is ``min(0xFFFF, 4 x w105C)`` (:func:`steady_state_bfs`);
* once the MAF filters have held, ``0xFFFF2100`` equals the airflow word
  ``0xFFFF1056`` the PID 0x10 / DID 0x1209 handler reports as ``w1056 x 149 >> 8``
  (both are ``w1054 + w273E`` at their fixed points; ``0xFFFF20E2`` is 0 by
  calibration), or one count below it where the non-transient airflow filter
  stalls, so the reported word bounds it to an interval
  (:func:`src.decode.air_charge.steady_airflow_interval`);
* the reported rpm word ``w1210`` bounds ``w1204`` to eight counts.

So :func:`predict_steady_bfs` turns one (MAF word, rpm word[, load byte])
into the exact interval of steady-state BFS the code can produce, every
calibration value read from the image. The chain is monotone (rising in
airflow and Fuel Comp, falling in speed), so the interval's ends are the
inputs' ends. With no logged ``0xFFFF118A`` Fuel Comp is the band of that rpm
row pair, which this calibration keeps within a few percent of 128.

:func:`evaluate_steady_bfs` pairs the raw telemetry itself (a pre-aggregated
frame cannot be handed to it) one-to-one on the acquisition clock
(:func:`pair_bfs_inputs`, :func:`src.signals.pairing.match_ordered`, BFS reads
as reference), applies the frozen steadiness gates
(:class:`SteadyGates`, declared before any scoring), and reports every BFS
read: scored with its per-sample ratio, or excluded with the reason. The
observed BFS is never an input: feeding it would validate only the X byte
downstream, which ``x_from_bfs`` already covers.

**Regions assumed, not read.** The branch flags behind three of the
application model's regions -- ``cranking`` (``0xFFFF4786``, ``0xFFFF2C91``,
``0xFFFF4A2D``), ``maf_fault`` (``0xFFFF2824`` bit 7) and
``fill_subtracting_form`` (``0xFFFF289C`` bit 5) -- have no catalogued
channel, so no capture reads them. Every scored record names them in
``regions_assumed`` and :meth:`SteadyBfsReport.summary` counts them: the score
is conditional on the engine running past the start branch (the 600 rpm
gate), no MAF fault and the normal filter form, never a check of them.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import numpy as np
import pandas as pd

from src.analysis.ecu_application import (
    ApplicationModel,
    ApplicationModelRefusalError,
    check_image,
)
from src.analysis.ecu_state_models import byte_curve16
from src.analysis.rom_map import RomMap, check_program_constants, read_program_constant
from src.decode import air_charge as kernel
from src.refusal import ReasonedRefusal
from src.signals.pairing import PairingPolicy, match_ordered

#: Bumped when a gate, a pairing rule or the prediction changes. 2: inputs
#: are paired one-to-one with ``match_ordered`` (was: nearest read, reuse
#: allowed; identical on the seven E05.2 drives and the 2026-10-04 RAM study).
EVALUATOR_VERSION = 2

#: Calibration bytes the Tp span and the MAF report read (cited on the
#: application model's nodes, which a test holds to this list).
FLOW_MULTIPLIER_OFFSET = 0x89D2
SPEED_MULTIPLIER_OFFSET = 0x88AD
MAF_REPORT_SCALE_OFFSET = 0x83E9
#: ``rom_map.yaml`` program constants this module checks against the kernel's.
PROGRAM_CONSTANTS = {
    "tp_quotient_cap": kernel.TP_QUOTIENT_CAP,
    "tp_scale": kernel.TP_SCALE,
}

#: Application-model regions whose flags no capture reads (module docstring):
#: each scored sample is counted as assuming it is outside them.
REGIONS_NOT_READ: tuple[str, ...] = ("cranking", "maf_fault", "fill_subtracting_form")

#: PID 0x0C reports the 12.5-rpm word ``w1210`` as ``w1210 x 50`` (rpm x 4).
PID_RPM_PER_WORD = 50
ECM_RPM_PER_COUNT = 12.5


class AirChargeRefusalError(ReasonedRefusal, ValueError):
    """An image, calibration or input the steady-state twin cannot use."""


@dataclass(frozen=True)
class AirChargeCalibration:
    """Every value the steady-state chain reads, from one image and slot."""

    image_sha256: str
    slot: int
    #: Slot ``+0x762`` (RAM ``0xFFFFB7A2``).
    k: int
    #: Cal ``0x89D2``.
    flow_multiplier: int
    #: Cal ``0x88AD``.
    speed_multiplier: int
    #: Cal ``0x83E9``: the PID 0x10 / DID 0x1209 report scale.
    maf_scale: int
    #: Slot ``+0x660``, 16 x 16 bytes, rows rpm.
    fuel_comp: tuple[tuple[int, ...], ...]
    #: ``0xCACD`` (rpm, raw bytes) and ``0xCB29`` (load byte, raw).
    fuel_comp_rpm_axis: tuple[int, ...]
    fuel_comp_load_axis: tuple[int, ...]
    #: ``0xD8B3``: the BFS ceiling curve (16 bytes, read at ``w1207``).
    ceiling: tuple[int, ...]

    @classmethod
    def from_image(
        cls, image: bytes, *, model: ApplicationModel, rom_map: RomMap, slot: int, image_sha256: str
    ) -> AirChargeCalibration:
        """Read the calibration after the model's program and calibration
        checks (``wrong_program``, ``patch_missing``, ``unmodelled_logic``)
        and the map's program literals (``program_constant_mismatch``)."""
        check_image(image, model)
        problems = check_program_constants(image, rom_map)
        if problems:
            raise AirChargeRefusalError("program_constant_mismatch", "; ".join(problems))
        by_id = {c.id: c for c in rom_map.program_constants}
        for constant_id, expected in PROGRAM_CONSTANTS.items():
            found = read_program_constant(image, by_id[constant_id])
            if found != expected:
                raise AirChargeRefusalError(
                    "program_constant_mismatch",
                    f"{constant_id} is 0x{found:X}; the kernel was decoded with 0x{expected:X}",
                )

        def cells(table_id: str) -> list[list[int]]:
            table = rom_map.table(table_id)
            base = rom_map.table_address(table, slot if table.slot_offset is not None else None)
            width = 2 if table.dtype == "be16" else 1
            out = []
            for r in range(table.rows):
                row = []
                for c in range(table.cols):
                    at = base + (r * table.cols + c) * width
                    row.append(int.from_bytes(image[at : at + width], "big"))
                out.append(row)
            return out

        return cls(
            image_sha256=image_sha256,
            slot=slot,
            k=cells("k_constant")[0][0],
            flow_multiplier=image[FLOW_MULTIPLIER_OFFSET],
            speed_multiplier=image[SPEED_MULTIPLIER_OFFSET],
            maf_scale=image[MAF_REPORT_SCALE_OFFSET],
            fuel_comp=tuple(tuple(r) for r in cells("fuel_compensation")),
            fuel_comp_rpm_axis=tuple(
                v for row in cells("fuel_comp_engine_speed_axis") for v in row
            ),
            fuel_comp_load_axis=tuple(v for row in cells("fuel_comp_flow_axis") for v in row),
            ceiling=tuple(v for row in cells("bfs_ceiling") for v in row),
        )


@dataclass(frozen=True)
class BfsPrediction:
    """The steady-state BFS interval for one input tuple, and its point."""

    lo: int
    hi: int
    #: The prediction at the inputs' lower-middle values: a representative
    #: point for a per-sample ratio, never a claim of more precision.
    mid: int
    fuel_comp_lo: int
    fuel_comp_hi: int
    airflow_lo: int
    airflow_hi: int
    speed_lo: int
    speed_hi: int

    @property
    def fuel_comp_exact(self) -> bool:
        return self.fuel_comp_lo == self.fuel_comp_hi


def _bfs(cal: AirChargeCalibration, airflow: int, speed: int, fuel_comp: int) -> int:
    tp = kernel.air_charge_tp(
        airflow,
        cal.k,
        speed,
        flow_multiplier=cal.flow_multiplier,
        speed_multiplier=cal.speed_multiplier,
    ).tp
    ceiling = byte_curve16(cal.ceiling, min(kernel.BYTE_MAX, speed >> 4))
    return kernel.steady_state_bfs(kernel.fuel_comp_product(tp, fuel_comp), ceiling)


@lru_cache(maxsize=4096)
def _fuel_comp_band(
    cells: tuple[tuple[int, ...], ...],
    rpm_axis: tuple[int, ...],
    load_axis: tuple[int, ...],
    rpm_word: int,
) -> tuple[int, int]:
    values = [
        kernel.rpm_load_lookup(cells, rpm_axis, load_axis, rpm_word, load)
        for load in range(kernel.BYTE_MAX + 1)
    ]
    return min(values), max(values)


def fuel_comp_interval(
    cal: AirChargeCalibration, rpm_word: int, load_byte: int | None
) -> tuple[int, int]:
    """The Fuel Comp byte at ``rpm_word``: exact from a logged load byte,
    else the exact range over every load byte the lookup can be given."""
    if load_byte is not None:
        value = kernel.rpm_load_lookup(
            cal.fuel_comp, cal.fuel_comp_rpm_axis, cal.fuel_comp_load_axis, rpm_word, load_byte
        )
        return value, value
    return _fuel_comp_band(
        cal.fuel_comp, cal.fuel_comp_rpm_axis, cal.fuel_comp_load_axis, int(rpm_word)
    )


def predict_steady_bfs(
    cal: AirChargeCalibration, *, maf_word: int, rpm_word: int, load_byte: int | None = None
) -> BfsPrediction:
    """Steady-state ``0xFFFF103A`` from the reported MAF word (PID 0x10 /
    DID 0x1209), the 12.5-rpm word and, when logged, ``0xFFFF118A``.

    Valid on the running branch, without a MAF fault, the transient term 0
    and the filters settled: the caller's gates decide when that holds.
    Refuses ``maf_word_unreachable`` for a word the handler cannot report."""
    try:
        airflow = kernel.steady_airflow_interval(maf_word, scale=cal.maf_scale)
    except ValueError as exc:
        raise AirChargeRefusalError("maf_word_unreachable", str(exc)) from exc
    speed = kernel.speed_1204_interval(rpm_word)
    fc_lo, fc_hi = fuel_comp_interval(cal, rpm_word, load_byte)
    return BfsPrediction(
        lo=_bfs(cal, airflow.lo, speed.hi, fc_lo),
        hi=_bfs(cal, airflow.hi, speed.lo, fc_hi),
        mid=_bfs(
            cal,
            (airflow.lo + airflow.hi) // 2,
            (speed.lo + speed.hi) // 2,
            (fc_lo + fc_hi) // 2,
        ),
        fuel_comp_lo=fc_lo,
        fuel_comp_hi=fc_hi,
        airflow_lo=airflow.lo,
        airflow_hi=airflow.hi,
        speed_lo=speed.lo,
        speed_hi=speed.hi,
    )


# -- raw words from stored telemetry ------------------------------------------------------


def _hex_word(row: Any) -> int | None:
    text = row.value_text
    return int(text, 16) if isinstance(text, str) and text else None


def _payload_word(row: Any) -> int | None:
    payload = row.payload
    if payload is None or len(payload) < 2:
        return None
    return (int(payload[0]) << 8) | int(payload[1])


def _rpm_word_from_pid(row: Any) -> int | None:
    word = _payload_word(row)
    if word is None or word % PID_RPM_PER_WORD:
        return None
    return word // PID_RPM_PER_WORD


def _whole_number(scale: float) -> Callable[[Any], int | None]:
    def extract(row: Any) -> int | None:
        value = row.value_number
        if value is None or not np.isfinite(value):
            return None
        raw = value / scale
        return int(round(raw)) if abs(raw - round(raw)) < 1e-6 else None

    return extract


#: How each channel's raw word is recovered, and which quantity it is. A
#: channel absent here is refused rather than guessed at.
EXTRACTORS: Mapping[str, tuple[str, Callable[[Any], int | None]]] = {
    "DID_1208": ("bfs", _hex_word),
    "mem:ECM_AIR_CHARGE_SLOT_103A": ("bfs", _whole_number(1.0)),
    "DID_1209": ("maf", _hex_word),
    "MAF": ("maf", _payload_word),
    "RPM": ("rpm", _rpm_word_from_pid),
    "mem:ECM_ENGINE_SPEED_RAM": ("rpm", _whole_number(ECM_RPM_PER_COUNT)),
    "mem:ECM_FUEL_COMP_X_INPUT": ("load", _whole_number(1.0)),
}


def raw_series(rows: pd.DataFrame, channel: str) -> tuple[np.ndarray, np.ndarray]:
    """``(acquired_at, word)`` for one channel, in acquisition order; rows
    whose raw word cannot be recovered exactly are dropped (and counted by
    :func:`evaluate_steady_bfs` through the missing pair)."""
    if channel not in EXTRACTORS:
        raise AirChargeRefusalError(
            "channel_unsupported", f"no raw-word rule for {channel!r} (known: {sorted(EXTRACTORS)})"
        )
    _, extract = EXTRACTORS[channel]
    times: list[float] = []
    words: list[int] = []
    for row in rows[rows["channel"] == channel].itertuples(index=False):
        word = extract(row)
        if word is None:
            continue
        times.append(_acquired(row))
        words.append(word)
    order = np.argsort(np.asarray(times, dtype=float), kind="stable")
    return np.asarray(times, dtype=float)[order], np.asarray(words, dtype=np.int64)[order]


def _acquired(row: Any) -> float:
    meta = getattr(row, "metadata_json", None)
    if isinstance(meta, str) and meta:
        acquired = json.loads(meta).get("acquired_at")
        if acquired is not None:
            return float(acquired)
    return float(pd.Timestamp(row.timestamp).timestamp())


# -- the frozen gates and the per-sample accounting ------------------------------------


@dataclass(frozen=True)
class SteadyGates:
    """Declared before any scoring (2026-10-05) and frozen: a test refuses a
    change. Steadiness is judged on the inputs only, never on BFS."""

    #: MAF read paired with a BFS read (same Mode 22 round in practice).
    max_maf_skew_s: float = 0.1
    max_rpm_skew_s: float = 0.25
    max_load_skew_s: float = 0.75
    #: Every input read within this many seconds either side must agree.
    window_s: float = 1.0
    max_maf_spread: float = 0.02
    max_rpm_spread: float = 0.01
    min_maf_reads_in_window: int = 3
    min_rpm: float = 600.0
    #: Acceptance: the per-sample ratio observed / predicted point.
    median_ratio_band: tuple[float, float] = (0.98, 1.02)
    p10_min: float = 0.95
    p90_max: float = 1.05
    min_scored: int = 200


#: The gates as declared; the default every evaluation uses.
FROZEN_GATES = SteadyGates()


@dataclass(frozen=True)
class BfsRecord:
    acquired_at: float
    observed: int
    prediction: BfsPrediction
    maf_word: int
    rpm_word: int
    load_byte: int | None
    #: Regions the sample is assumed to be outside because their flags were
    #: not read (:data:`REGIONS_NOT_READ`).
    regions_assumed: tuple[str, ...] = REGIONS_NOT_READ

    @property
    def ratio(self) -> float:
        return self.observed / self.prediction.mid

    @property
    def inside(self) -> bool:
        return self.prediction.lo <= self.observed <= self.prediction.hi


@dataclass(frozen=True)
class SteadyBfsReport:
    records: tuple[BfsRecord, ...]
    #: Every BFS read not scored, by reason (each read counted once).
    excluded: dict[str, int] = field(default_factory=dict)
    gates: SteadyGates = FROZEN_GATES

    @property
    def total(self) -> int:
        return len(self.records) + sum(self.excluded.values())

    def summary(self) -> dict[str, Any]:
        ratios = sorted(r.ratio for r in self.records)
        if not ratios:
            return {"scored": 0, "excluded": dict(self.excluded)}
        q = (
            statistics.quantiles(ratios, n=10, method="inclusive")
            if len(ratios) > 1
            else ratios * 9
        )
        return {
            "scored": len(ratios),
            "excluded": dict(self.excluded),
            "median_ratio": statistics.median(ratios),
            "p10_ratio": q[0],
            "p90_ratio": q[-1],
            "inside_interval": sum(r.inside for r in self.records) / len(self.records),
            "fuel_comp_exact": sum(r.prediction.fuel_comp_exact for r in self.records),
            "regions_assumed": {
                region: sum(region in r.regions_assumed for r in self.records)
                for region in REGIONS_NOT_READ
            },
        }

    def passes(self) -> bool:
        s = self.summary()
        g = self.gates
        if s["scored"] < g.min_scored:
            return False
        lo, hi = g.median_ratio_band
        return bool(
            lo <= s["median_ratio"] <= hi
            and s["p10_ratio"] >= g.p10_min
            and s["p90_ratio"] <= g.p90_max
        )


@dataclass(frozen=True)
class BfsSeries:
    """The raw words one BFS evaluation reads, each channel in acquisition
    order (:func:`raw_series`). ``load_*`` are empty when no load channel is
    read; ``has_load`` says which."""

    bfs_t: np.ndarray
    bfs_w: np.ndarray
    maf_t: np.ndarray
    maf_w: np.ndarray
    rpm_t: np.ndarray
    rpm_w: np.ndarray
    load_t: np.ndarray
    load_w: np.ndarray
    has_load: bool


def bfs_series(
    rows: pd.DataFrame,
    *,
    bfs_channel: str = "DID_1208",
    maf_channel: str = "DID_1209",
    rpm_channel: str = "RPM",
    load_channel: str | None = None,
) -> BfsSeries:
    """The raw words of ``rows`` for one BFS evaluation, after each channel is
    checked to carry the quantity it is read as (``channel_unsupported``)."""
    for channel, quantity in (
        (bfs_channel, "bfs"),
        (maf_channel, "maf"),
        (rpm_channel, "rpm"),
        (load_channel, "load"),
    ):
        if channel is not None and EXTRACTORS.get(channel, ("", None))[0] != quantity:
            raise AirChargeRefusalError(
                "channel_unsupported", f"{channel!r} is not a {quantity} channel"
            )
    bfs_t, bfs_w = raw_series(rows, bfs_channel)
    maf_t, maf_w = raw_series(rows, maf_channel)
    rpm_t, rpm_w = raw_series(rows, rpm_channel)
    load_t, load_w = raw_series(rows, load_channel) if load_channel else (np.empty(0), np.empty(0))
    return BfsSeries(
        bfs_t, bfs_w, maf_t, maf_w, rpm_t, rpm_w, load_t, load_w, has_load=load_channel is not None
    )


def score_bfs_read(
    series: BfsSeries,
    index: int,
    cal: AirChargeCalibration,
    *,
    maf_index: int | None,
    rpm_index: int | None,
    load_index: int | None,
    gates: SteadyGates = FROZEN_GATES,
) -> BfsRecord | str:
    """One BFS read (``series.bfs_t[index]``) scored, or the name of the gate
    that excludes it. The pairing is the caller's: ``*_index`` is the input
    read paired with it within the gate's skew, or ``None`` when none is.
    Gates apply in a fixed order, so each read has exactly one reason."""
    at, observed = series.bfs_t[index], series.bfs_w[index]
    if maf_index is None:
        return "maf_unpaired"
    if rpm_index is None:
        return "rpm_unpaired"
    maf, rpm = int(series.maf_w[maf_index]), int(series.rpm_w[rpm_index])
    if rpm * ECM_RPM_PER_COUNT < gates.min_rpm:
        return "below_min_rpm"
    near_maf = series.maf_w[np.abs(series.maf_t - at) <= gates.window_s]
    if near_maf.size < gates.min_maf_reads_in_window:
        return "maf_window_sparse"
    if np.max(np.abs(near_maf - maf)) > gates.max_maf_spread * maf:
        return "maf_not_steady"
    near_rpm = series.rpm_w[np.abs(series.rpm_t - at) <= gates.window_s]
    if np.max(np.abs(near_rpm - rpm)) > gates.max_rpm_spread * rpm:
        return "rpm_not_steady"
    load: int | None = None
    if series.has_load:
        if load_index is None:
            return "load_unpaired"
        load = int(series.load_w[load_index])
    try:
        prediction = predict_steady_bfs(cal, maf_word=maf, rpm_word=rpm, load_byte=load)
    except AirChargeRefusalError as exc:
        return exc.reason
    if prediction.mid == 0:
        return "predicted_zero"
    return BfsRecord(float(at), int(observed), prediction, maf, rpm, load)


@dataclass(frozen=True)
class BfsPairs:
    """Per BFS read (by index into ``series.bfs_t``), the index of the input
    read paired with it; a read absent from a map has no partner in skew."""

    maf: dict[int, int]
    rpm: dict[int, int]
    load: dict[int, int]


def _match(reference: np.ndarray, other: np.ndarray, max_skew_s: float) -> dict[int, int]:
    if not len(reference) or not len(other):
        return {}
    matched = match_ordered(
        reference, other, PairingPolicy.within(pd.Timedelta(seconds=max_skew_s), reference="a")
    )
    return dict(zip(matched.index_a.tolist(), matched.index_b.tolist(), strict=True))


def pair_bfs_inputs(series: BfsSeries, gates: SteadyGates = FROZEN_GATES) -> BfsPairs:
    """Each input paired one-to-one with the BFS reads on the acquisition
    clock (:func:`~src.signals.pairing.match_ordered`, BFS reads as reference,
    each within its gate's skew): an input read is never placed in two
    samples."""
    return BfsPairs(
        maf=_match(series.bfs_t, series.maf_t, gates.max_maf_skew_s),
        rpm=_match(series.bfs_t, series.rpm_t, gates.max_rpm_skew_s),
        load=_match(series.bfs_t, series.load_t, gates.max_load_skew_s),
    )


def evaluate_steady_bfs(
    rows: pd.DataFrame,
    cal: AirChargeCalibration,
    *,
    gates: SteadyGates = FROZEN_GATES,
    bfs_channel: str = "DID_1208",
    maf_channel: str = "DID_1209",
    rpm_channel: str = "RPM",
    load_channel: str | None = None,
) -> SteadyBfsReport:
    """Score every stored BFS read of ``rows`` (raw telemetry rows: channel,
    value_number, value_text, payload, metadata_json, timestamp) against
    :func:`predict_steady_bfs` from the paired MAF and rpm reads. Every scored
    record carries ``regions_assumed`` (module docstring): the cranking,
    MAF-fault and subtracting-filter flags are not read, so the score assumes
    each sample is outside those regions and says so.

    Inputs are paired by :func:`pair_bfs_inputs`, the pairing
    :mod:`src.analysis.twin_runner` uses for the same reads."""
    series = bfs_series(
        rows,
        bfs_channel=bfs_channel,
        maf_channel=maf_channel,
        rpm_channel=rpm_channel,
        load_channel=load_channel,
    )
    pairs = pair_bfs_inputs(series, gates)
    records: list[BfsRecord] = []
    excluded: dict[str, int] = {}
    for index in range(len(series.bfs_t)):
        scored = score_bfs_read(
            series,
            index,
            cal,
            maf_index=pairs.maf.get(index),
            rpm_index=pairs.rpm.get(index),
            load_index=pairs.load.get(index),
            gates=gates,
        )
        if isinstance(scored, str):
            excluded[scored] = excluded.get(scored, 0) + 1
        else:
            records.append(scored)
    return SteadyBfsReport(records=tuple(records), excluded=excluded, gates=gates)


__all__ = [
    "EVALUATOR_VERSION",
    "EXTRACTORS",
    "FROZEN_GATES",
    "REGIONS_NOT_READ",
    "AirChargeCalibration",
    "AirChargeRefusalError",
    "ApplicationModelRefusalError",
    "BfsPrediction",
    "BfsPairs",
    "BfsRecord",
    "BfsSeries",
    "SteadyBfsReport",
    "SteadyGates",
    "bfs_series",
    "evaluate_steady_bfs",
    "fuel_comp_interval",
    "pair_bfs_inputs",
    "predict_steady_bfs",
    "raw_series",
    "score_bfs_read",
]
