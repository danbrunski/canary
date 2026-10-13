"""B08.1 — every derived channel's declared inputs are exactly what it reads.

The artifact-lineage audit (:mod:`src.analysis.artifact_lineage`) traces a
model's ``derived:`` features through the *literal* input declarations it parses
at the build's git revision. Two things must hold for that to mean anything:

1. **Every declaration is literal**: the parser, run on the working tree,
   locates every channel in :data:`DERIVED_CHANNEL_INPUTS` and
   :data:`DERIVED_CHANNEL_OPTIONAL_INPUTS`, with the imported values.
2. **The declarations are the computation's inputs**, checked by running the
   real derivation chain (the pipeline's own step order) on frames built from
   the declaration:

   - *nothing undeclared is read*: for every frame built from the declared
     inputs, adding every other column of the universe leaves the channel
     bit-identical;
   - *every declared input is read*: for each declared leaf there is such a
     frame from which removing that one column changes the channel or stops
     it being produced.

   Frames are the declaration's minimal conjunctive choices (one member per
   group; ``min_members`` of a committee's members), each also with every
   optional input added one at a time. Derived members expand to their own
   frames, so the check is at the leaf columns.

``decoded:`` inputs that today's bindings catalog cannot scale (``decoded:MAF``
and kin, declared for a vehicle that binds them) are read at scale 1 here; the
real chain skips them only because no binding exists.

The undeclared columns tried are every column a catalog or collector can put
in a frame: the Mode 01 registry, ``dids.yaml`` DIDs and RAM channels, the
broadcast bindings, the GPS fix fields, IMU, baro and the analog sensor. Each
sweeps its catalogued range where one is stated (``resolve_limits``).

Blind spots, stated plainly; this test does **not** prove every read is caught:

- *Value-conditional reads.* A read that happens only under a condition the
  synthetic rows never meet goes unseen. Single-channel extremes are covered
  where a catalog states a range (rpm sweeps 0-7,000, so a read above 5,000
  rpm is caught), but each column sweeps independently over 120 rows, so a
  read gated on a *joint* condition (high rpm *and* low load *and* cold
  coolant) may never be triggered. Channels with no catalogued range sweep
  10-100, and a few shaped channels (the A/F voltage, base fuel schedule,
  narrowband O2, PID 0x44, baro, the GPS gates) hold realistic shapes rather
  than their full range.
- *Columns no catalog names* (a typo'd or invented channel) cannot be in any
  frame, so a read of one is unseen here, and is a no-op at runtime too.
- *Leaf comparison.* A group naming a derived channel whose leaves are
  already declared through another group (the AFR error's censor flag) is not
  distinguished from its absence.

Producibility is checked per context (D03.41): every minimal frame of the
declaration must produce the channel, because each is a column set the
registry's consumers (capture-profile validation, the serving gate) accept as
sufficient. Measured AFR declared its A/F voltage DID as sufficient while the
voltage route withholds every row without a fresh baro (B06.7), and the old
"some frame produces it" check passed it.
"""

from __future__ import annotations

import itertools
import zlib
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.analysis.artifact_lineage import parse_derived_sources
from src.collection.capture_profiles import CaptureProfile, channel_satisfied
from src.config import VehicleParameters
from src.decode import engineering
from src.decode.dids import DidMap, load_repo_did_map
from src.decode.engineering import UnscaledDecodedChannelError, repo_bindings
from src.features.feature_pipeline import FeaturePolicy
from src.plausibility.limits import load_plausibility_profile, resolve_limits
from src.preprocessing.channel_units import append_did_views, channel_units_for
from src.preprocessing.imu_decimation import IMU_TICK_COLUMNS
from src.signals import derived
from src.signals.afr import append_afr_channels, load_afr_calibration
from src.signals.committees import COMMITTEES, append_committee_residuals
from src.signals.derived import (
    DERIVED_CHANNEL_INPUTS,
    DERIVED_CHANNEL_OPTIONAL_INPUTS,
    compute_anomaly_signals,
    compute_derived_channels,
)
from src.vehicle.tune_revisions import load_tune_revisions

REPO = Path(__file__).resolve().parents[1]
XTERRA = REPO / "data" / "nissan-xterra-2008"
ROWS = 120
ROW_COLUMNS = ("session", "timestamp")
BARO = "obd:BAROMETRIC_PRESSURE"

Registry = Mapping[str, tuple[tuple[str, ...], ...]]
Optional = Mapping[str, tuple[str, ...]]

VEHICLE = VehicleParameters(
    final_drive=3.357,
    gearbox_ratios=(3.79, 2.28, 1.45, 1.0, 0.82),
    tire_loaded_circumference_m=2.4,
    driveline_evidence="synthetic test vehicle",
)
#: Committee outputs whose first group is "any ``min_members`` of these".
MIN_MEMBERS = {committee.output: committee.min_members for committee in COMMITTEES}


# ---------------------------------------------------------------------------
# 1. The parser locates every declaration
# ---------------------------------------------------------------------------


def _working_tree_definitions():  # type: ignore[no-untyped-def]
    vehicles = sorted((REPO / "src/signals/vehicle_signals").glob("*.py"))
    return parse_derived_sources(
        (REPO / "src/signals/derived.py").read_text(),
        [path.read_text() for path in vehicles if path.name != "__init__.py"],
    )


def test_the_lineage_parser_locates_every_derived_declaration_on_the_working_tree() -> None:
    """Each entry, required and optional, is found literally with its imported
    value, so a build from this tree is traceable by the audit (B08.1)."""
    parsed = _working_tree_definitions()
    imported = {name: tuple(map(tuple, groups)) for name, groups in DERIVED_CHANNEL_INPUTS.items()}
    assert sorted(set(imported) - set(parsed.inputs)) == []
    assert {name: parsed.inputs[name] for name in imported} == imported
    optional = {name: tuple(c) for name, c in DERIVED_CHANNEL_OPTIONAL_INPUTS.items()}
    assert dict(parsed.optional) == optional
    # The channels the 2026-10-05 audit could not locate at the deployed builds.
    for channel in (
        "derived:AFR_B1",
        "derived:AFR_CENSORED_B1",
        "derived:FUEL_RATE_LPH",
        "derived:ESTIMATED_POWER_HP",
        "derived:ESTIMATED_TORQUE_NM",
        "derived:INSTANT_ECONOMY_L_PER_100KM",
        "derived:INSTANT_ECONOMY_MPG",
    ):
        assert channel in parsed.inputs, channel


def test_a_vehicle_literal_that_disagrees_with_the_code_refuses_at_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The literal table is checked against what the code builds, so the audit
    can never parse a declaration the computation does not use."""
    from src.signals.vehicle_signals import nissan_xterra_2008

    wrong = dict(nissan_xterra_2008.DERIVED_CHANNEL_INPUTS)
    wrong["derived:AFR_B1"] = (("obd:O2_WR_LAMBDA_S1",),)
    monkeypatch.setattr(nissan_xterra_2008, "DERIVED_CHANNEL_INPUTS", wrong)
    from src.signals import vehicle_signals

    with pytest.raises(derived.DerivedDeclarationError, match="derived:AFR_B1"):
        derived._require_literal(
            "the vehicle-built derived inputs",
            derived._VEHICLE_INPUTS,
            vehicle_signals.literal_derived_inputs(),
            "test",
        )


# ---------------------------------------------------------------------------
# 2. The declarations are what the computation reads
# ---------------------------------------------------------------------------


#: Channels whose values need a shape, not just a range: narrowband O2 must
#: switch (catalyst efficiency counts crossings), the A/F voltage must sit in
#: the transfer's domain, base fuel schedule on the target map's axis, PID 0x44
#: must show decel fuel cut (exactly 0), and the GPS gates must hold.
def _shaped(column: str, t: np.ndarray) -> np.ndarray | None:
    wave = np.sin(t / 7.0)
    shaped: dict[str, np.ndarray] = {
        "obd:O2_B1S1": 0.45 + 0.4 * np.sign(np.sin(t)),
        "obd:O2_B2S1": 0.45 + 0.4 * np.sign(np.cos(t)),
        "obd:O2_B1S2": 0.45 + 0.4 * np.sign(np.sin(t / 4.0)),
        "obd:O2_B2S2": 0.45 + 0.4 * np.sign(np.cos(t / 5.0)),
        "did:ECM_AF_SENSOR_V_B1": 1.5 + 0.2 * np.sin(t / 4.0),
        "did:ECM_AF_SENSOR_V_B2": 1.45 + 0.2 * np.cos(t / 6.0),
        "did:ECM_AIR_CHARGE": (9.0 + 12.0 * (0.5 + 0.5 * np.sin(t / 9.0))) / 0.00049,
        "obd:COMMANDED_EQUIV_RATIO": np.where(t % 7 == 3, 0.0, 1.05 + 0.08 * wave),
        "obd:BAROMETRIC_PRESSURE": 80.0 + 0.5 * np.sin(t / 20.0),
        "gps:num_satellites": np.full(len(t), 10.0),
        "gps:hdop": np.full(len(t), 0.9),
    }
    return shaped.get(column)


def _leaf_values(column: str, t: np.ndarray, span: tuple[float, float] | None) -> np.ndarray:
    """Moving values for one input column, in its arriving units (raw counts
    for a ``decoded:`` column).

    Where a catalog states the channel's range (``plausibility.yaml``, the
    bindings and DID catalogs, the J1979 encoding range;
    :func:`src.plausibility.limits.resolve_limits`), the values sweep the whole
    of it, so a read that only happens at high rpm or full load is exercised.
    Each column sweeps at its own period and phase, so their extremes meet in
    different combinations; not every combination occurs."""
    shaped = _shaped(column, t)
    if shaped is not None:
        return shaped
    seed = zlib.crc32(column.encode())
    rng = np.random.default_rng(seed)
    period = 17.0 + seed % 41
    phase = (seed >> 8) % 97
    triangle = np.abs(((t + phase) / period) % 2.0 - 1.0)
    if span is None:
        return 10.0 + 90.0 * triangle + rng.normal(0, 1, len(t))
    low, high = span
    return low + (high - low) * triangle


def _catalog_channels(did_map: DidMap) -> frozenset[str]:
    """Every column a catalog or collector can put in a wide frame: the Mode 01
    registry, ``dids.yaml`` DIDs and RAM channels, the broadcast bindings, the
    GPS fix fields, the IMU and baro channels and the analog sensor."""
    from src.collection.obd_registry import MODE01_REGISTRY

    bindings = repo_bindings()
    assert bindings is not None
    return frozenset(
        {
            *(f"obd:{definition.channel}" for definition in MODE01_REGISTRY.values()),
            *(f"did:{entry.name}" for entry in did_map.dids),
            *(f"mem:{entry.name}" for entry in did_map.memory),
            *(f"decoded:{signal.name}" for signal in bindings.signals),
            *(
                f"gps:{field}"
                for field in (
                    "fix_quality",
                    "num_satellites",
                    "hdop",
                    "speed",
                    "heading",
                    "latitude",
                    "longitude",
                    "altitude",
                )
            ),
            *IMU_TICK_COLUMNS,
            "imu:ACCEL_LON",
            "imu:ACCEL_LAT",
            "imu:GYRO_Z",
            "baro:pressure",
            "baro:altitude",
            "sensor:FUEL_PRESSURE",
        }
    )


def _spans(did_map: DidMap) -> dict[str, tuple[float, float]]:
    """``channel -> (low, high)`` in arriving units, where a catalog states both."""
    limits = resolve_limits(
        load_plausibility_profile(XTERRA / "plausibility.yaml"),
        bindings=repo_bindings(),
        did_map=did_map,
    )
    return {
        channel: (found.minimum.value, found.maximum.value)
        for channel, found in limits.channels.items()
        if found.minimum is not None and found.maximum is not None
    }


def _leaves(channel: str, registry: Registry, optional: Optional) -> frozenset[str]:
    """Every non-derived column ``channel`` declares, transitively, optional included."""
    out: set[str] = set()
    for member in {m for group in registry[channel] for m in group} | set(
        optional.get(channel, ())
    ):
        out |= _leaves(member, registry, optional) if member in registry else {member}
    return frozenset(out)


def _declared(channel: str, registry: Registry, optional: Optional) -> frozenset[str]:
    """``channel`` and every column its declaration names, transitively."""
    out = {channel}
    for member in {m for group in registry[channel] for m in group} | set(
        optional.get(channel, ())
    ):
        out |= _declared(member, registry, optional) if member in registry else {member}
    return frozenset(out)


def _optional_leaves(channel: str, registry: Registry, optional: Optional) -> frozenset[str]:
    out = set(optional.get(channel, ()))
    for member in {m for group in registry[channel] for m in group}:
        if member in registry:
            out |= _optional_leaves(member, registry, optional)
    return frozenset(out)


def _minimal(sets: set[frozenset[str]]) -> list[frozenset[str]]:
    return sorted(
        (s for s in sets if not any(other < s for other in sets)), key=lambda s: sorted(s)
    )


def _contexts(channel: str, registry: Registry) -> list[frozenset[str]]:
    """The minimal column sets satisfying ``channel``'s required groups."""

    def member(column: str) -> list[frozenset[str]]:
        return _contexts(column, registry) if column in registry else [frozenset({column})]

    options: list[list[frozenset[str]]] = []
    for index, group in enumerate(registry[channel]):
        k = MIN_MEMBERS.get(channel) if index == 0 else None
        if k is None:
            options.append([c for m in group for c in member(m)])
        else:
            options.append(
                [
                    frozenset().union(*choice)
                    for combo in itertools.combinations(group, k)
                    for choice in itertools.product(*(member(m) for m in combo))
                ]
            )
    return _minimal({frozenset().union(*choice) for choice in itertools.product(*options)})


def _covering(frames: list[frozenset[str]]) -> list[frozenset[str]]:
    """Few of ``frames`` that between them hold every column any of them
    holds (greedy), so each declared path is run once with the decoys."""
    remaining = set().union(*frames) if frames else set()
    chosen: list[frozenset[str]] = []
    while remaining:
        best = max(frames, key=lambda f: (len(f & remaining), -len(f)))
        chosen.append(best)
        remaining -= best
    return chosen


def _real_physical_series():  # type: ignore[no-untyped-def]
    return engineering.physical_series


@pytest.fixture(scope="module")
def chain() -> Iterator[_Chain]:
    real = _real_physical_series()

    def readable(values: pd.Series, column: str, bindings: object = None) -> pd.Series:
        try:
            return real(values, column, bindings)  # type: ignore[arg-type]
        except UnscaledDecodedChannelError:
            return pd.to_numeric(values, errors="coerce").astype(float)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(engineering, "physical_series", readable)
        patch.setattr(derived, "physical_series", readable)
        yield _Chain()


class _Chain:
    """The pipeline's derivation steps, in its order
    (``feature_pipeline._append_derived_channels``, with the offline trip
    integrals), memoised by column set and run only as far as the step that
    produces the channel asked for: a later step cannot change it."""

    def __init__(self) -> None:
        self.did_map = load_repo_did_map()
        assert self.did_map is not None
        self.units = channel_units_for(self.did_map)
        self.calibration = load_afr_calibration(XTERRA / "afr_calibration.yaml")
        flash = load_tune_revisions(XTERRA / "tune_revisions.yaml")
        assert flash is not None
        self.flash = flash
        t = np.arange(ROWS, dtype=float)
        # Dated 2026-09-24, on a revision whose target map is recorded.
        rows = pd.DataFrame(
            {
                "session": "2026-09-24T120000",
                "timestamp": pd.date_range(
                    datetime(2026, 9, 24, 12, tzinfo=UTC), periods=ROWS, freq="s"
                ),
            }
        )
        declared = set().union(
            *(
                _leaves(c, DERIVED_CHANNEL_INPUTS, DERIVED_CHANNEL_OPTIONAL_INPUTS)
                for c in DERIVED_CHANNEL_INPUTS
            )
        )
        # Every column a catalog can produce is a candidate undeclared read.
        channels = declared | _catalog_channels(self.did_map)
        self.channels = frozenset(channels)
        # No ``__gap`` flag means no input is stale. The baro read arrives on
        # every row, so the gain's age check (B06.7) sees a fresh read; the
        # other channels carry no flags, which only keeps the frame narrow.
        spans = _spans(self.did_map)
        columns: dict[str, np.ndarray] = {
            channel: _leaf_values(channel, t, spans.get(channel)) for channel in sorted(channels)
        }
        columns[f"{BARO}__arrived"] = np.ones(ROWS, dtype=bool)
        self.universe = pd.concat([rows, pd.DataFrame(columns)], axis=1)
        self._memo: dict[tuple[frozenset[str], int], pd.DataFrame] = {}
        self._widened: dict[tuple[frozenset[str], frozenset[str], int], pd.DataFrame] = {}
        self._step_of: dict[str, int] = {}
        for step in range(len(self.steps)):
            for column in self.run(self.channels, step).columns:
                self._step_of.setdefault(str(column), step)

    @property
    def steps(self) -> tuple[Callable[[pd.DataFrame], pd.DataFrame], ...]:
        return (
            lambda frame: append_did_views(frame, did_map=self.did_map),
            lambda frame: append_afr_channels(
                frame,
                VEHICLE,
                self.calibration,
                channel_units=self.units,
                flash_history=self.flash,
            ),
            lambda frame: compute_anomaly_signals(frame, VEHICLE),
            lambda frame: compute_derived_channels(frame, VEHICLE, calibration=self.calibration),
            append_committee_residuals,
        )

    def run(self, channels: frozenset[str], upto: int) -> pd.DataFrame:
        """The frame of ``channels`` after steps ``0..upto`` (``-1``: none)."""
        key = (channels, upto)
        if key not in self._memo:
            if upto < 0:
                wanted = [
                    c
                    for c in self.universe.columns
                    if c in ROW_COLUMNS or str(c).rsplit("__", 1)[0] in channels
                ]
                self._memo[key] = self.universe.loc[:, wanted].copy()
            else:
                self._memo[key] = self.steps[upto](self.run(channels, upto - 1))
        return self._memo[key]

    def forget(self) -> None:
        """Drop every memoised run (after the computation was patched)."""
        self._memo.clear()
        self._widened.clear()

    def step_of(self, channel: str) -> int:
        return self._step_of.get(channel, len(self.steps) - 1)

    def value(self, channel: str, channels: frozenset[str]) -> pd.Series | None:
        return _present(self.run(channels, self.step_of(channel)), channel)

    def widened(
        self, channel: str, channels: frozenset[str], declared: frozenset[str]
    ) -> pd.Series | None:
        """``channel`` with every undeclared column beside ``channels``: each
        undeclared raw channel, and every derived column the earlier steps
        produce from the whole universe, except ``declared`` (the channel's own
        declaration, derived members included, which come from ``channels``).

        Only the producing step runs on the wide frame; the earlier ones run on
        ``channels``. Running them wide too would only recompute, at many
        times the cost, columns that are added here anyway."""
        step = self.step_of(channel)
        key = (channels, declared, step)
        if key not in self._widened:
            frame = self.run(channels, step - 1)
            full = self.run(self.channels, step - 1)
            extra = [
                c
                for c in full.columns
                if c not in frame.columns
                and c not in ROW_COLUMNS
                and str(c).rsplit("__", 1)[0] not in declared
            ]
            wide = pd.concat([frame, full.loc[:, extra]], axis=1)
            self._widened[key] = self.steps[step](wide)
        return _present(self._widened[key], channel)


def _present(frame: pd.DataFrame, channel: str) -> pd.Series | None:
    if channel not in frame.columns or not frame[channel].notna().any():
        return None
    return frame[channel]


def _same(a: pd.Series | None, b: pd.Series | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return a.equals(b)


def _culprits(
    candidates: list[str],
    value_with: Callable[[frozenset[str]], pd.Series | None],
    alone: pd.Series | None,
) -> list[str]:
    """The single columns among ``candidates`` whose addition changes the value,
    found by halving (a read only a combination triggers is not named)."""
    if not candidates or _same(alone, value_with(frozenset(candidates))):
        return []
    if len(candidates) == 1:
        return candidates
    middle = len(candidates) // 2
    return _culprits(candidates[:middle], value_with, alone) + _culprits(
        candidates[middle:], value_with, alone
    )


def declaration_problems(
    channel: str, chain: _Chain, registry: Registry, optional: Optional
) -> list[str]:
    """Where ``channel``'s declaration (in ``registry`` / ``optional``) and its
    computation disagree; empty when they are the same inputs.

    - Every frame satisfying the required groups minimally, and each of those
      with one column removed, must give the same value with every undeclared
      column of the universe added (*nothing undeclared is read*, including an
      undeclared alternative that stands in for a removed one).
    - Each declared column must change the value when removed from one of
      those frames or from one with an optional input added (*every declared
      input is read*).
    - Every minimal frame must produce the channel (*each context the
      declaration calls sufficient is*; D03.41).
    """
    leaves = _leaves(channel, registry, optional)
    declared = _declared(channel, registry, optional)
    decoys = chain.channels - leaves
    base = _contexts(channel, registry)
    variants = [
        context | {extra}
        for extra in sorted(_optional_leaves(channel, registry, optional))
        for context in base
        if extra not in context
    ]
    problems: list[str] = []
    checked: set[frozenset[str]] = set()

    def undeclared(columns: frozenset[str]) -> None:
        if columns in checked:
            return
        checked.add(columns)
        alone = chain.value(channel, columns)
        if _same(alone, chain.widened(channel, columns, declared)):
            return
        read = _culprits(sorted(decoys), lambda some: chain.value(channel, columns | some), alone)
        problems.append(
            f"reads undeclared input(s) {read or '(a combination)'} beside {sorted(columns)}"
        )

    frames = [*base, *variants]
    produced = any(chain.value(channel, columns) is not None for columns in frames)
    # Producibility (D03.41): every minimal frame of the declaration is a
    # context the registry calls sufficient (capture-profile validation and the
    # serving gate accept a profile carrying just that frame), so each must
    # produce the channel.
    unproducible = [columns for columns in base if chain.value(channel, columns) is None]
    witnessed = {
        leaf
        for leaf in leaves
        if any(
            not _same(chain.value(channel, columns), chain.value(channel, columns - {leaf}))
            for columns in frames
            if leaf in columns
        )
    }
    for columns in _covering(base):
        undeclared(columns)
    # Each required column removed from the first minimal frame holding it,
    # with every optional input present: an undeclared column standing in for
    # the removed one would now be read.
    optional_leaves = _optional_leaves(channel, registry, optional)
    for leaf in sorted(leaves - optional_leaves):
        holding = next((columns for columns in base if leaf in columns), None)
        if holding is not None:
            undeclared((holding - {leaf}) | optional_leaves)
    if not produced:
        problems.append("never produced from its declared inputs")
    else:
        problems += [
            f"not produced from the minimal frame {sorted(columns)}" for columns in unproducible
        ]
    problems += [f"declares {c} but never reads it" for c in sorted(leaves - witnessed)]
    return problems


def _families() -> list[tuple[str, ...]]:
    """The registry's channels grouped by the columns they read, so channels
    built on one another (fuel rate, fuel used, CO2) share their frames."""
    by_leaves: dict[frozenset[str], list[str]] = {}
    for channel in sorted(DERIVED_CHANNEL_INPUTS):
        leaves = _leaves(channel, DERIVED_CHANNEL_INPUTS, DERIVED_CHANNEL_OPTIONAL_INPUTS)
        by_leaves.setdefault(leaves, []).append(channel)
    return sorted(tuple(family) for family in by_leaves.values())


@pytest.mark.parametrize(
    "family", _families(), ids=lambda family: "+".join(c.removeprefix("derived:") for c in family)
)
def test_each_derived_channel_reads_exactly_its_declared_inputs(
    family: tuple[str, ...], chain: _Chain
) -> None:
    problems = {
        channel: declaration_problems(
            channel, chain, DERIVED_CHANNEL_INPUTS, DERIVED_CHANNEL_OPTIONAL_INPUTS
        )
        for channel in family
    }
    assert {channel: found for channel, found in problems.items() if found} == {}


def test_every_registry_channel_is_checked() -> None:
    assert sorted(c for family in _families() for c in family) == sorted(DERIVED_CHANNEL_INPUTS)


def test_an_undeclared_input_is_caught(chain: _Chain) -> None:
    """Dropping the bank-1-sensor-2 lambda from the fuel rate's optional
    inputs: the MAF method still divides by it when sensor 1 is absent."""
    optional = dict(DERIVED_CHANNEL_OPTIONAL_INPUTS)
    optional["derived:FUEL_RATE_LPH"] = tuple(
        c for c in optional["derived:FUEL_RATE_LPH"] if c != "obd:O2_WR_LAMBDA_S2"
    )
    problems = declaration_problems(
        "derived:FUEL_RATE_LPH", chain, DERIVED_CHANNEL_INPUTS, optional
    )
    assert any("undeclared input(s) ['obd:O2_WR_LAMBDA_S2']" in p for p in problems), problems


def _with_patched_fuel_rate(
    chain: _Chain,
    monkeypatch: pytest.MonkeyPatch,
    reads: Callable[[pd.DataFrame, pd.Series], pd.Series],
) -> list[str]:
    """``declaration_problems`` for the fuel rate while its computation also
    reads ``reads(frame, fuel_g_s)`` — an undeclared input planted in the code."""
    real = derived._instant_fuel_g_s

    def planted(wide: pd.DataFrame, *args: object, **kwargs: object) -> pd.Series | None:
        fuel = real(wide, *args, **kwargs)  # type: ignore[arg-type]
        return None if fuel is None else reads(wide, fuel)

    monkeypatch.setattr(derived, "_instant_fuel_g_s", planted)
    chain.forget()
    try:
        return declaration_problems(
            "derived:FUEL_RATE_LPH", chain, DERIVED_CHANNEL_INPUTS, DERIVED_CHANNEL_OPTIONAL_INPUTS
        )
    finally:
        monkeypatch.undo()
        chain.forget()


def test_an_undeclared_read_of_any_catalogued_column_is_caught(
    chain: _Chain, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decoys are every catalogued column, not a hand-picked few: a fuel
    rate that scales by the tank level (Mode 01 PID 0x2F, ``obd:FUEL_LEVEL``,
    which nothing declares) fails. The verification probe named
    ``obd:FUEL_TANK_LEVEL``, which no catalog or collector produces, so no
    frame can carry it; the real channel is the one planted here."""
    assert "obd:FUEL_TANK_LEVEL" not in chain.channels
    assert "obd:FUEL_LEVEL" in chain.channels

    def tank(wide: pd.DataFrame, fuel: pd.Series) -> pd.Series:
        if "obd:FUEL_LEVEL" not in wide:
            return fuel
        return fuel * (1.0 + pd.to_numeric(wide["obd:FUEL_LEVEL"]) / 100.0)

    problems = _with_patched_fuel_rate(chain, monkeypatch, tank)
    assert any("['obd:FUEL_LEVEL']" in p for p in problems), problems


def test_an_undeclared_read_at_high_rpm_only_is_caught(
    chain: _Chain, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inputs sweep their catalogued range, so a read that happens only above
    5,000 rpm is exercised (rpm sweeps 0-7,000, plausibility.yaml)."""

    def high_rpm(wide: pd.DataFrame, fuel: pd.Series) -> pd.Series:
        if "obd:COOLANT_TEMP" not in wide or "obd:RPM" not in wide:
            return fuel
        rpm = pd.to_numeric(wide["obd:RPM"])
        coolant = pd.to_numeric(wide["obd:COOLANT_TEMP"])
        return fuel.where(rpm <= 5000.0, fuel * (1.0 + coolant / 1000.0))

    problems = _with_patched_fuel_rate(chain, monkeypatch, high_rpm)
    assert any("'obd:COOLANT_TEMP'" in p for p in problems), problems


def test_a_declared_input_the_computation_ignores_is_caught(chain: _Chain) -> None:
    registry = dict(DERIVED_CHANNEL_INPUTS)
    registry["derived:BOOST_VACUUM_KPA"] = (
        *registry["derived:BOOST_VACUUM_KPA"],
        ("obd:COOLANT_TEMP",),
    )
    problems = declaration_problems(
        "derived:BOOST_VACUUM_KPA", chain, registry, DERIVED_CHANNEL_OPTIONAL_INPUTS
    )
    assert "declares obd:COOLANT_TEMP but never reads it" in problems


def test_a_missing_alternative_is_caught(chain: _Chain) -> None:
    """The A/F voltage fallback removed from measured AFR: read, undeclared.

    Baro stays in the frame (as an optional input) so the voltage route can
    run without the lambda PID; with neither, the undeclared read needs two
    columns at once and is reported only as "a combination" (D03.41 made
    baro a required input of that route)."""
    registry = dict(DERIVED_CHANNEL_INPUTS)
    registry["derived:AFR_B1"] = (("obd:O2_WR_LAMBDA_S1",),)
    optional = {**DERIVED_CHANNEL_OPTIONAL_INPUTS, "derived:AFR_B1": (BARO,)}
    problems = declaration_problems("derived:AFR_B1", chain, registry, optional)
    assert any("did:ECM_AF_SENSOR_V_B1" in p for p in problems), problems


#: D03.41's declaration of measured AFR before the fix: the A/F voltage DID
#: alone named sufficient, baro only optional.
_PRE_D03_41_AFR_B1 = (("obd:O2_WR_LAMBDA_S1", "did:ECM_AF_SENSOR_V_B1"),)


def test_a_declared_context_that_cannot_produce_the_channel_is_caught(chain: _Chain) -> None:
    """D03.41: with baro merely optional, the minimal frame of the voltage DID
    alone is declared sufficient, but the voltage route withholds every row
    without a fresh baro, so that frame produces nothing."""
    registry = {**DERIVED_CHANNEL_INPUTS, "derived:AFR_B1": _PRE_D03_41_AFR_B1}
    optional = {**DERIVED_CHANNEL_OPTIONAL_INPUTS, "derived:AFR_B1": (BARO,)}
    problems = declaration_problems("derived:AFR_B1", chain, registry, optional)
    assert "not produced from the minimal frame ['did:ECM_AF_SENSOR_V_B1']" in problems, problems


#: The D03.41 repro: a tuning-style profile polling the A/F voltage DIDs but
#: neither Mode 01 baro nor the wide-range lambda PIDs.
_VOLTAGE_ONLY = CaptureProfile(
    name="repro",
    storage_prefix="_capture-repro",
    mode01={"RPM": 1.0},
    mode22=["ECM_AF_SENSOR_V_B1", "ECM_AF_SENSOR_V_B2", "ECM_AIR_CHARGE"],
)


@pytest.mark.parametrize(
    "channel",
    [
        "derived:AFR_B1",
        "derived:AFR_CENSORED_B1",
        "derived:AFR_ERROR_B1",
        "derived:AFR_B2",
        "derived:AFR_CENSORED_B2",
        "derived:AFR_ERROR_B2",
        "derived:LAMBDA_BANK_BALANCE_AF_VOLTAGE_BARO",
    ],
)
def test_a_profile_without_baro_or_lambda_cannot_supply_measured_afr(channel: str) -> None:
    """Capture-profile validation and the serving gate read the registry's
    groups, so the conjunctive declaration makes both refuse a profile that
    could never produce measured AFR (D03.41)."""
    assert not channel_satisfied(channel, _VOLTAGE_ONLY, frozenset())
    policy = FeaturePolicy(
        bindings=None,
        serving_mode01=frozenset({"RPM"}),
        serving_dids=frozenset({"ECM_AF_SENSOR_V_B1", "ECM_AF_SENSOR_V_B2", "ECM_AIR_CHARGE"}),
        did_map=load_repo_did_map(),
    )
    assert policy._unserved_group(channel, frozenset()) is not None


@pytest.mark.parametrize(
    "mode01",
    [
        {"RPM": 1.0, "BAROMETRIC_PRESSURE": 1.0},
        {"RPM": 1.0, "O2_WR_LAMBDA_S1": 1.0, "O2_WR_LAMBDA_S5": 1.0},
    ],
    ids=["voltage-and-baro", "lambda"],
)
def test_either_route_supplies_measured_afr(mode01: dict[str, float]) -> None:
    """The stricter declaration still accepts both real routes: the lambda PID,
    or the A/F voltage with baro."""
    profile = CaptureProfile(
        name="routes", storage_prefix="_capture-routes", mode01=mode01, mode22=_VOLTAGE_ONLY.mode22
    )
    for bank in (1, 2):
        assert channel_satisfied(f"derived:AFR_B{bank}", profile, frozenset())
        assert channel_satisfied(f"derived:AFR_CENSORED_B{bank}", profile, frozenset())
