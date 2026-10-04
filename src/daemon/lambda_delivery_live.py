"""Lambda delivery error as a live tuning readout (B06.4).

Every declared (commanded source, measured source) pair in the vehicle's
``lambda_delivery.yaml`` becomes one route. Each route's reads are taken off
:attr:`LiveFeatureSource.record_observers` on their own acquisition clock and
decided by :class:`~src.analysis.lambda_delivery_stream.DeliveryStream`, which
calls the offline engine's own row judge. On a stored drive the readout gives
exactly the rows :func:`~src.analysis.lambda_delivery.delivery_samples` gives
(``tests/test_lambda_delivery_live.py`` checks it on real tuning captures).

**A readout, not a detector.** It never emits an anomaly event, feeds no
fusion and has no alert role: B06.5 has no held-out enrichment evidence, so
no delivery threshold is validated and nothing here may claim a fault. It
therefore is **not gated by the capture profile (R03.12)**. That gate keeps
tuning captures out of the Isolation Forest, the sequence detector and the
behavior residuals because those score against a *learned* normal a different
polling set invalidates. This readout learns nothing — it compares two
declared instruments under a declared rule — and the tuning captures are the
only sessions that carry the enrichment reads it exists to show. Nor is it
gated by vehicle state: with the engine off there are no target reads, and
the route says so.

**Every empty result says which kind.** Per route: ``inputs_not_observed``
(names the specs this session never delivered — a profile that polls neither,
not a finding), ``pending`` (reads arrived, none decided yet), ``not_co_observed``
(both sides read, never within the acquisition separation),
``clock_unsupported`` (round-stamped reads cannot carry a 250 ms pairing),
``no_eligible_rows`` (the declared eligibility removed every row — an answer
about the drive, with the state counts that say why), or ``eligible``.

The tick is the daemon's 1 Hz inference tick, but only as the publication
cadence: no row is a one-second cell, and each is published about
``settle + tolerance + delay`` seconds after its target read. The state
restarts on a session change (a new recording directory, a replay starting
or stopping), as the offline engine analyses one session at a time.
"""

from __future__ import annotations

import logging
import math
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
import pandas as pd

from src.analysis.channels import parse_spec
from src.analysis.lambda_delivery import (
    ELIGIBLE,
    STATES,
    JudgedRows,
    LambdaDeliveryDeclaration,
    regime_noise_floor,
    state_counts,
)
from src.analysis.lambda_delivery_stream import DEFAULT_SETTLE_S, DeliveryStream
from src.analysis.noise_floor import NoiseFloor
from src.decode.dids import FOREIGN_DID_SOURCES, DidEntry, DidMap, decode_raw_did_reading
from src.signals.afr import AfrCalibration
from src.signals.pairing import to_seconds
from src.telemetry import TelemetryRecord

if TYPE_CHECKING:
    from src.daemon.components import DaemonComponents
    from src.daemon.inference_metrics import InferenceTimingRecorder
    from src.features.live import LiveFeatureSource

log = logging.getLogger(__name__)

STAGE_NAME = "lambda_delivery"
#: What this block is, stated in the payload so no consumer mistakes it for
#: a detector verdict.
ROLE = "tuning_readout"
NOT_AN_ALERT = (
    "a tuning-support readout, not an anomaly detector: no delivery model is validated "
    "(B06.5 has no held-out steady enrichment evidence), so it never raises an event, "
    "and it runs under every capture profile because the tuning captures are the ones "
    "that carry enrichment (R03.12 gates learned-normal detectors only)"
)

Result = Literal[
    "withheld",
    "inputs_not_observed",
    "clock_unsupported",
    "pending",
    "not_co_observed",
    "no_eligible_rows",
    "eligible",
]
_REGIMES = ("closed_loop", "enrichment")


class UnsupportedSpecError(ValueError):
    """A declared spec the live record stream cannot supply."""


# ---------------------------------------------------------------------------
# Reading declared specs off raw records, as the offline reader does


def _number(value: object) -> float | None:
    """A stored ``value_number``: a real non-NaN number (inf kept, as stored)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return None if math.isnan(number) else number


@dataclass(frozen=True)
class _SpecReader:
    """How one declared spec appears on the live record stream.

    Mirrors :func:`src.analysis.channels.read_channels`: ``obd:`` is a
    case-insensitive channel match on a number; ``mem:`` is the collector's
    ``mem:<NAME>`` channel, whose non-numeric rows (the fuel-cut sentinel)
    read as a 1.0 flag; ``did:`` is the raw ``DID_<hex>`` reading decoded
    through the catalog.
    """

    spec: str
    kind: Literal["obd", "mem", "did"]
    channel: str
    entry: DidEntry | None = None

    def value(self, record: TelemetryRecord) -> float | None:
        if self.kind == "obd":
            return _number(record.value)
        if self.kind == "mem":
            number = _number(record.value)
            if number is not None:
                return number
            return 1.0 if isinstance(record.value, str) else None
        if record.source in FOREIGN_DID_SOURCES or self.entry is None:
            return None
        decoded = decode_raw_did_reading(
            {f"{self.entry.did:04X}": self.entry},
            record.channel,
            record.value,
            source=record.source,
        )
        return None if decoded is None else decoded.value


def spec_reader(spec: str, did_map: DidMap | None) -> _SpecReader:
    """The live reader for ``spec``; refuses a source the stream cannot supply."""
    parsed = parse_spec(spec)
    if parsed.source == "obd":
        return _SpecReader(spec, "obd", parsed.name)
    if parsed.source == "mem":
        return _SpecReader(spec, "mem", f"mem:{parsed.name}")
    if parsed.source == "did":
        entry = None
        if did_map is not None:
            entry = next((e for e in did_map.dids if e.name == parsed.name), None)
        if entry is None:
            raise UnsupportedSpecError(f"{spec}: not in the DID catalog the daemon loaded")
        return _SpecReader(spec, "did", f"DID_{entry.did:04X}", entry)
    raise UnsupportedSpecError(
        f"{spec}: a {parsed.source!r} channel is not read by the live delivery readout "
        "(polled obd:/mem:/did: channels only)"
    )


def _basis(record: TelemetryRecord) -> tuple[float | None, str]:
    """``(acquired_at, basis)`` as the offline reader derives them."""
    acquired = _number(record.metadata.get("acquired_at"))
    if acquired is not None:
        return acquired, "acquired"
    if _number(record.metadata.get("round_id")) is not None:
        return None, "round"
    return None, "stored"


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)


def _stored_seconds(stamp: datetime) -> float:
    """A record's stored stamp as :func:`to_seconds` would give it, exactly.

    Integer microseconds, split into whole seconds and fraction as
    ``to_seconds`` splits nanoseconds, so no float rounding separates the
    live clock from the offline one.
    """
    micros = (stamp - _EPOCH) // _MICROSECOND
    return float(micros // 1_000_000) + float(micros % 1_000_000) / 1e6


@dataclass
class _Pending:
    """Reads of one spec since the last tick, not yet on the float clock."""

    acquired: list[float] = field(default_factory=list)
    stored: list[float] = field(default_factory=list)
    values: list[float] = field(default_factory=list)

    def clear(self) -> None:
        self.acquired, self.stored, self.values = [], [], []


def _take_all(pending: dict[str, _Pending]) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Every spec's pending reads as ``(float seconds, values)``, then cleared.

    ``acquired_at`` goes through ``pd.to_datetime(unit="s")`` exactly as the
    offline reader converts it (one call for every spec's reads); a read
    without one keeps its stored stamp, as offline.
    """
    specs = [spec for spec, reads in pending.items() if reads.values]
    out = {spec: (np.array([], dtype=float), np.array([], dtype=float)) for spec in pending}
    if not specs:
        return out
    sizes = [len(pending[spec].values) for spec in specs]
    acquired = np.concatenate([np.asarray(pending[spec].acquired, dtype=float) for spec in specs])
    times = np.concatenate([np.asarray(pending[spec].stored, dtype=float) for spec in specs])
    has = ~np.isnan(acquired)
    if has.any():
        times[has] = to_seconds(pd.to_datetime(pd.Series(acquired[has]), unit="s", utc=True))
    start = 0
    for spec, size in zip(specs, sizes, strict=True):
        out[spec] = (times[start : start + size], np.asarray(pending[spec].values, dtype=float))
        pending[spec].clear()
        start += size
    return out


# ---------------------------------------------------------------------------
# One route: its stream and its session's accumulated rows


@dataclass
class _Route:
    stream: DeliveryStream
    target_spec: str
    sentinel_spec: str | None
    measured_spec: str
    counts: Counter[str] = field(default_factory=Counter)
    paired: int = 0
    residuals: dict[str, list[float]] = field(
        default_factory=lambda: {regime: [] for regime in _REGIMES}
    )
    latest: dict[str, Any] | None = None
    latest_eligible: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Per-regime residual statistics, recomputed only when rows were added.
    _stats: dict[str, dict[str, Any]] = field(default_factory=dict)

    def stats(self, regime: str) -> dict[str, Any]:
        cached = self._stats.get(regime)
        if cached is None:
            cached = self._stats[regime] = _stats(self.residuals[regime])
        return cached

    def reset(self) -> None:
        self.stream.reset()
        self.counts = Counter()
        self.paired = 0
        self.residuals = {regime: [] for regime in _REGIMES}
        self.latest = None
        self.latest_eligible = {}
        self._stats = {}

    def fold(self, judged: JudgedRows) -> None:
        if not len(judged.rows):
            return
        self.counts.update(state_counts(judged.state))
        self.paired += int(np.isfinite(judged.separation_s).sum())
        eligible = judged.state == ELIGIBLE
        for regime in _REGIMES:
            chosen = eligible & (judged.regime == regime)
            if chosen.any():
                self.residuals[regime].extend(float(v) for v in judged.residual_pct[chosen])
                self._stats.pop(regime, None)
                self.latest_eligible[regime] = _row(judged, int(np.flatnonzero(chosen)[-1]))
        self.latest = _row(judged, len(judged.rows) - 1)


def _finite(value: float) -> float | None:
    return float(value) if math.isfinite(value) else None


def _row(judged: JudgedRows, i: int) -> dict[str, Any]:
    """One decided row, as the readout publishes it (B06.6's fields together)."""
    return {
        "timestamp": datetime.fromtimestamp(float(judged.times[i]), tz=UTC).isoformat(),
        "target_lambda": _finite(judged.target_lambda[i]),
        "measured_lambda": _finite(judged.measured_lambda[i]),
        "residual_pct": _finite(judged.residual_pct[i]),
        "regime": str(judged.regime[i]) or None,
        "state": str(judged.state[i]),
        "separation_s": _finite(judged.separation_s[i]),
    }


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "median_pct": None, "p05_pct": None, "p95_pct": None}
    array = np.asarray(values, dtype=float)
    return {
        "n": int(len(array)),
        "median_pct": float(np.median(array)),
        "p05_pct": float(np.quantile(array, 0.05)),
        "p95_pct": float(np.quantile(array, 0.95)),
    }


# ---------------------------------------------------------------------------
# The readout


class LambdaDeliveryReadout:
    """Every declared route, fed from raw records and decided per tick."""

    def __init__(
        self,
        declaration: LambdaDeliveryDeclaration,
        *,
        calibration: AfrCalibration | None,
        afr_stoich: float,
        noise_floor: NoiseFloor | None = None,
        did_map: DidMap | None = None,
        declaration_path: Path | None = None,
        settle_s: float = DEFAULT_SETTLE_S,
        session: str = "live",
        on_rows: Callable[[str, str, JudgedRows], None] | None = None,
    ) -> None:
        self.declaration = declaration
        #: Called with ``(target_source, measured_source, rows)`` for every
        #: batch of decided rows, e.g. to compare them with the offline engine.
        self.on_rows = on_rows
        self.noise_floor = noise_floor
        self.declaration_path = declaration_path
        self.settle_s = settle_s
        self.session = session
        self.routes: list[_Route] = []
        self.withheld: list[dict[str, Any]] = []
        self._readers: dict[str, _SpecReader] = {}
        for commanded in declaration.commanded_sources:
            for measured in declaration.measured_sources:
                specs = [commanded.spec, measured.spec]
                if commanded.sentinel_spec is not None:
                    specs.append(commanded.sentinel_spec)
                try:
                    readers = [spec_reader(spec, did_map) for spec in specs]
                except UnsupportedSpecError as exc:
                    self.withheld.append(
                        {
                            "target_source": commanded.name,
                            "measured_source": measured.name,
                            "result": "withheld",
                            "reason": str(exc),
                        }
                    )
                    continue
                for reader in readers:
                    self._readers[reader.spec] = reader
                self.routes.append(
                    _Route(
                        stream=DeliveryStream(
                            declaration,
                            target_source=commanded.name,
                            measured_source=measured.name,
                            calibration=calibration,
                            afr_stoich=afr_stoich,
                            settle_s=settle_s,
                        ),
                        target_spec=commanded.spec,
                        sentinel_spec=commanded.sentinel_spec,
                        measured_spec=measured.spec,
                    )
                )
        # Exact channel names for mem:/DID_ reads, case-folded for obd PIDs
        # (the offline obd reader matches case-insensitively).
        self._exact: dict[str, list[_SpecReader]] = {}
        self._folded: dict[str, list[_SpecReader]] = {}
        for reader in self._readers.values():
            if reader.kind == "obd":
                self._folded.setdefault(reader.channel.casefold(), []).append(reader)
            else:
                self._exact.setdefault(reader.channel, []).append(reader)
        self._reset_reads()

    def _reset_reads(self) -> None:
        self._pending: dict[str, _Pending] = {spec: _Pending() for spec in self._readers}
        self._bases: dict[str, Counter[str]] = {spec: Counter() for spec in self._readers}
        self.clock_s = -math.inf

    def reset(self, session: str) -> None:
        """A new drive: forget every read and row."""
        self.session = session
        self._reset_reads()
        for route in self.routes:
            route.reset()

    # -- the record hook ----------------------------------------------------

    def observe(self, record: TelemetryRecord) -> None:
        """One raw record (every record the live source receives)."""
        if record.source != "obd":
            return
        readers = self._exact.get(record.channel) or self._folded.get(record.channel.casefold())
        if not readers:
            return
        acquired, basis = _basis(record)
        for reader in readers:
            value = reader.value(record)
            if value is None:
                continue
            pending = self._pending[reader.spec]
            pending.acquired.append(math.nan if acquired is None else acquired)
            pending.stored.append(_stored_seconds(record.timestamp))
            pending.values.append(value)
            self._bases[reader.spec][basis] += 1

    # -- the tick ------------------------------------------------------------

    def _clock_unsupported(self, route: _Route) -> list[str]:
        specs = [route.target_spec, route.measured_spec]
        if route.sentinel_spec is not None:
            specs.append(route.sentinel_spec)
        return [spec for spec in specs if self._bases[spec]["round"]]

    def tick(self) -> None:
        """Move this tick's reads onto the streams and decide what can be."""
        taken = _take_all(self._pending)
        for times, _values in taken.values():
            if len(times):
                self.clock_s = max(self.clock_s, float(np.max(times)))
        horizon = self.clock_s - self.settle_s
        for route in self.routes:
            route.stream.extend(
                target=taken[route.target_spec],
                sentinel=None if route.sentinel_spec is None else taken[route.sentinel_spec][0],
                measured=taken[route.measured_spec],
            )
            if self._clock_unsupported(route):
                continue
            self._fold(route, route.stream.advance(horizon))

    def flush(self) -> None:
        """Decide every remaining row with the reads there are — what the
        offline engine does at the end of a stored drive."""
        self.tick()
        for route in self.routes:
            if not self._clock_unsupported(route):
                self._fold(route, route.stream.flush())

    def _fold(self, route: _Route, judged: JudgedRows) -> None:
        route.fold(judged)
        if self.on_rows is not None and len(judged.rows):
            self.on_rows(route.stream.commanded.name, route.stream.instrument.name, judged)

    # -- publication ---------------------------------------------------------

    def _route_result(self, route: _Route) -> tuple[Result, str, list[str]]:
        seen = route.stream.reads_seen
        missing = [
            spec
            for spec, kind in ((route.target_spec, "target"), (route.measured_spec, "measured"))
            if not seen[kind]
        ]
        round_specs = self._clock_unsupported(route)
        total = int(route.counts.get("total", 0))
        if round_specs:
            return (
                "clock_unsupported",
                "round-stamped reads with no acquired_at cannot carry the declared "
                f"{route.stream.tolerance_s * 1000:g} ms pairing: " + ", ".join(round_specs),
                missing,
            )
        if missing:
            return (
                "inputs_not_observed",
                "this session has delivered no reading of " + ", ".join(missing) + " — the "
                "polling profile does not supply this route, which is not a delivery result",
                missing,
            )
        if total == 0:
            return (
                "pending",
                f"reads arrived; rows are decided {route.stream.settle_s:g} s after their "
                "pairing window closes on the acquisition clock",
                missing,
            )
        if route.paired == 0:
            return (
                "not_co_observed",
                "both sides were read, but no target read has a measured read within the "
                "declared acquisition separation",
                missing,
            )
        if route.counts.get(ELIGIBLE, 0) == 0:
            return (
                "no_eligible_rows",
                "the declared eligibility removed every row — an answer about this drive; "
                "the state counts say which rule did",
                missing,
            )
        return "eligible", "rows carry a delivery residual", missing

    def _route_snapshot(self, route: _Route) -> dict[str, Any]:
        stream = route.stream
        result, reason, missing = self._route_result(route)
        rules = self.declaration.eligibility.regimes
        regimes: dict[str, Any] = {}
        for regime in _REGIMES:
            delay = rules.delay(regime, stream.commanded.name)
            regimes[regime] = {
                **route.stats(regime),
                "source_valid_in_regime": regime in stream.commanded.valid_regimes,
                "transport_delay_modelled": delay.status == "frozen",
                "transport_delay_s": stream.delays_s[regime],
                "latest_eligible": route.latest_eligible.get(regime),
                "noise_floor": regime_noise_floor(self.noise_floor, regime, self.declaration),
            }
        counts = {name: int(route.counts.get(name, 0)) for name in STATES}
        counts["total"] = int(route.counts.get("total", 0))
        return {
            "target_source": stream.commanded.name,
            "target_spec": route.target_spec,
            "measured_source": stream.instrument.name,
            "measured_spec": route.measured_spec,
            "result": result,
            "reason": reason,
            "missing_inputs": missing,
            "states": counts,
            "paired": route.paired,
            "regimes": regimes,
            "latest": route.latest,
            "undecided_rows": stream.undecided,
            "reads_seen": dict(stream.reads_seen),
            # A read that arrived after a row depending on it was published:
            # the one way this readout can differ from the offline engine.
            "late_reads": dict(stream.late_reads),
            "measured_disagreements": [
                item.model_dump()
                for item in self.declaration.disagreements_for(stream.instrument.name)
            ],
        }

    def snapshot(self) -> dict[str, Any]:
        declaration = self.declaration
        return {
            "enabled": True,
            "role": ROLE,
            "alerts": False,
            "capture_profile_gated": False,
            "vehicle_state_gated": False,
            "validated_model": False,
            "why_not_an_alert": NOT_AN_ALERT,
            "declaration": None if self.declaration_path is None else str(self.declaration_path),
            "session": self.session,
            "residual": declaration.residual.definition,
            "residual_positive": declaration.residual.positive,
            "pairing": {
                "method": "acquisition-time match_ordered",
                "engine": "src.analysis.lambda_delivery.judge_target_rows",
                "max_separation_s": declaration.acquisition.max_separation_s,
                "clock": "acquired_at where present, else the stored stamp",
                "evaluation": (
                    "published on the 1 Hz inference tick; each row is one target read on "
                    "the acquisition clock, never a one-second cell"
                ),
                "settle_s": self.settle_s,
                "claims_instantaneous_target_tracking": False,
                "tracking_note": (
                    "a residual exists only on a target that held its regime's declared "
                    "dwell; a moving target is the target_transient state"
                ),
            },
            "routes": [self._route_snapshot(route) for route in self.routes] + self.withheld,
        }


# ---------------------------------------------------------------------------
# Daemon wiring


def _session(components: DaemonComponents) -> str:
    # Lazy, as committees_live does: plausibility_live pulls its evaluator.
    from src.daemon.plausibility_live import live_session_key

    session, _replay_id = live_session_key(components)
    return session


class LiveLambdaDelivery:
    """The readout on the live tick: session scoping, cost and ``/status``."""

    def __init__(
        self,
        readout: LambdaDeliveryReadout,
        *,
        recorder: InferenceTimingRecorder | None,
    ) -> None:
        self.readout = readout
        self._recorder = recorder
        self._last_tick_ms = 0.0
        self._max_tick_ms = 0.0
        self.observe_failures = 0

    def attach(self, source: LiveFeatureSource) -> None:
        readout = self.readout

        def observe(record: TelemetryRecord) -> None:
            try:
                readout.observe(record)
            except Exception:  # noqa: BLE001 — a readout must never cost a record
                self.observe_failures += 1
                log.exception("lambda delivery readout failed to observe a record")

        source.record_observers.append(observe)

    def tick(self, *, session: str) -> None:
        if session != self.readout.session:
            self.readout.reset(session)
        started = time.perf_counter()
        self.readout.tick()
        seconds = time.perf_counter() - started
        self._last_tick_ms = seconds * 1000.0
        self._max_tick_ms = max(self._max_tick_ms, self._last_tick_ms)
        if self._recorder is not None:
            self._recorder.record_stage(STAGE_NAME, seconds)
            self._recorder.set_lambda_delivery(self.snapshot())

    def tick_live(self, components: DaemonComponents) -> None:
        """:meth:`tick`, scoped to the daemon's current session/replay."""
        self.tick(session=_session(components))

    def snapshot(self) -> dict[str, Any]:
        block = self.readout.snapshot()
        block["cost"] = {
            "last_tick_ms": round(self._last_tick_ms, 3),
            "max_tick_ms": round(self._max_tick_ms, 3),
            "observe_failures": self.observe_failures,
        }
        return block


def build_live_lambda_delivery(
    components: DaemonComponents,
    source: LiveFeatureSource,
    recorder: InferenceTimingRecorder,
    *,
    context_factory: Callable[[Any], Any] | None = None,
) -> LiveLambdaDelivery | None:
    """The live readout, or ``None`` (``/status`` says which kind of none).

    Built from the vehicle's :class:`~src.analysis.tuning_context.TuningContext`
    — the one place the declaration, AFR calibration, stoichiometry and noise
    floor are resolved (R05.1). Never raises into the inference loop.
    """
    from src.analysis.tuning_context import TuningContext

    factory = context_factory if context_factory is not None else TuningContext.from_config
    try:
        context = factory(components.config)
    except Exception as exc:  # noqa: BLE001 — a malformed file must not stop scoring
        log.warning("lambda delivery readout withheld: %s", exc)
        recorder.set_lambda_delivery(
            {"enabled": False, "result": "withheld", "reason": f"{type(exc).__name__}: {exc}"}
        )
        return None
    if context.lambda_delivery is None:
        recorder.set_lambda_delivery(
            {
                "enabled": False,
                "result": "not_declared",
                "reason": (
                    "this vehicle profile has no lambda_delivery.yaml, so nothing declares "
                    "the commanded target, the measured instrument or how fuel cut shows"
                ),
            }
        )
        return None
    did_map = source.did_map
    if did_map is None:
        from src.decode.dids import load_did_map
        from src.paths import DIDS_YAML

        did_map = load_did_map(DIDS_YAML)
    readout = LambdaDeliveryReadout(
        context.lambda_delivery,
        calibration=context.calibration,
        afr_stoich=context.afr_stoich,
        noise_floor=context.noise_floor,
        did_map=did_map,
        declaration_path=context.lambda_delivery_path,
        session=_session(components),
    )
    live = LiveLambdaDelivery(readout, recorder=recorder)
    live.attach(source)
    recorder.set_lambda_delivery(live.snapshot())
    return live


__all__ = [
    "NOT_AN_ALERT",
    "ROLE",
    "STAGE_NAME",
    "LambdaDeliveryReadout",
    "LiveLambdaDelivery",
    "UnsupportedSpecError",
    "build_live_lambda_delivery",
    "spec_reader",
]
