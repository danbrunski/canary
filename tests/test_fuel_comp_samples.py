"""N02.3: every fuelling-error candidate in exactly one named state.

A synthetic session (built here, acquisition-stamped) exercises each state
and gate on its own, with invented load-byte tables so no private ROM is
needed. The B06 delivery fixture then runs through this path and must give
B06's committed numbers: the open-loop route is ``delivery_samples``, not a
second implementation of it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.analysis.fuel_comp_inputs import FuelCompDeclaration, load_fuel_comp_declaration
from src.analysis.fuel_comp_rom import (
    FuelCompCalibration,
    FuelCompRomRefusalError,
    RevisionImageResolver,
)
from src.analysis.fuel_comp_samples import (
    COLUMNS,
    STATES,
    SessionSamples,
    eligible_summary,
    session_samples,
)
from src.analysis.lambda_delivery import (
    ELIGIBLE,
    LambdaDeliveryDeclaration,
    load_lambda_delivery,
)
from src.analysis.rom_map import load_rom_map
from src.decode.fuel_comp_load import LoadInputCurves
from src.preprocessing.loader import read_session
from src.signals.afr import AfrCalibration, load_afr_calibration
from src.signals.derived import SHARED_ANOMALY_SIGNALS
from src.vehicle.tune_revisions import TuneRevision, TuneRevisionRegistry
from tests.rom_fixtures import PROFILE, load_byte_image

B06_SLICE = Path("tests/fixtures/lambda_delivery/_capture-tuning-2026-09-21T223734")
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def declaration() -> FuelCompDeclaration:
    return load_fuel_comp_declaration(PROFILE)


@pytest.fixture(scope="module")
def delivery() -> LambdaDeliveryDeclaration:
    return load_lambda_delivery(
        PROFILE / "lambda_delivery.yaml",
        dids_path=Path("data/dids.yaml"),
        providers_path=PROFILE / "tuning_providers.yaml",
    )


@pytest.fixture(scope="module")
def afr() -> AfrCalibration:
    return load_afr_calibration(PROFILE / "afr_calibration.yaml")


def _calibration(name: str = "synthetic") -> FuelCompCalibration:
    """Invented tables: rising curves, a flat Fuel Comp, the 20261004-shape axes."""
    curves = LoadInputCurves(
        area_breakpoints=tuple(100 * i for i in range(32)),
        area_values=tuple(200 + 30 * i * i for i in range(32)),
        # Steep first segment, as a fill curve is: small X spans many bytes.
        fill_low=(0, *(16000 + 2400 * i for i in range(15))),
        fill_high=tuple(49600 + 500 * i for i in range(16)),
        rpm_multiplier=150,
        reciprocal_numerator=10_000_000,
        idle_air_cap=1000,
        limp_home_area=700,
    )
    load_axis = tuple(range(16, 256, 16))
    return FuelCompCalibration(
        name=name,
        image_sha256="0" * 64,
        source_path=Path("synthetic.bin"),
        image_format="plain",
        slot=1,
        fuel_comp=tuple((128,) * 16 for _ in range(16)),
        rpm_axis_raw=tuple(range(8, 136, 8)),
        load_axis_raw=(*load_axis, 255),
        rpm_breakpoints=tuple(4 * b for b in range(8, 136, 8)),
        k=28000,
        purge_gain=128,
        curves=curves,
        table_ids={"fuel_comp": "synthetic"},
    )


def _registry(*flashes: tuple[str, datetime]) -> TuneRevisionRegistry:
    return TuneRevisionRegistry(
        revisions=[TuneRevision(revision=name, flashed_at=when) for name, when in flashes]
    )


def _resolver(refuse: dict[str, str] | None = None) -> Callable[[str], FuelCompCalibration]:
    refuse = refuse or {}

    def resolve(name: str) -> FuelCompCalibration:
        if name in refuse:
            raise FuelCompRomRefusalError(refuse[name], f"{name} refused for the test")
        return _calibration(name)

    return resolve


def _session(seconds: int = 120, step: float = 1.0, **overrides: Any) -> pd.DataFrame:
    """A steady warm closed-loop cruise, one read of each channel per ``step``
    seconds, every read of a step within 120 ms of the others.

    ``overrides`` replace a channel's value (a constant, or a function of the
    time in seconds; ``None`` means no read), add a channel (``DID_*``
    channels are stored as payload hex, the way Mode 22 rows are), or move a
    channel's reads with ``<CHANNEL>_offset``.
    """
    base: dict[str, Any] = {
        "RPM": 2000.0,
        "THROTTLE_POS": 51 * 100 / 255,
        "SHORT_FUEL_TRIM_1": 1.5625,
        "LONG_FUEL_TRIM_1": 3.125,
        "SHORT_FUEL_TRIM_2": -0.78125,
        "LONG_FUEL_TRIM_2": 2.34375,
        "FUEL_STATUS": 2.0,
        "COOLANT_TEMP": 90.0,
        "BAROMETRIC_PRESSURE": 80.0,
        "COMMANDED_EQUIV_RATIO": 1.0,
        "O2_WR_LAMBDA_S1": 1.0,
    }
    base.update({k: v for k, v in overrides.items() if not k.endswith("_offset")})
    offsets = {name: 0.01 * i for i, name in enumerate(base)}
    rows = []
    for index in range(int(round(seconds / step))):
        t = index * step
        for name, value in base.items():
            v = value(t) if callable(value) else value
            if v is None:
                continue
            offset = overrides.get(f"{name}_offset", offsets[name])
            o = offset(t) if callable(offset) else offset
            stamp = T0.timestamp() + t + o
            did = name.startswith("DID_")
            rows.append(
                {
                    "timestamp": pd.Timestamp(stamp, unit="s", tz="UTC"),
                    "source": "obd",
                    "channel": name,
                    "value_number": None if did else float(v),
                    "value_text": f"{int(v):04X}" if did else None,
                    "unit": "",
                    "metadata_json": json.dumps({"acquired_at": stamp}),
                }
            )
    return pd.DataFrame(rows)


def _run(
    frame: pd.DataFrame,
    declaration: FuelCompDeclaration,
    delivery: LambdaDeliveryDeclaration,
    afr: AfrCalibration,
    **kwargs: Any,
) -> SessionSamples:
    return session_samples(
        frame,
        declaration=declaration,
        delivery=delivery,
        afr_calibration=afr,
        afr_stoich=14.7,
        registry=kwargs.pop("registry", _registry(("synthetic", datetime(2026, 8, 1, tzinfo=UTC)))),
        resolver=kwargs.pop("resolver", _resolver()),
        session_name="synthetic-session",
        **kwargs,
    )


def _closed(result: SessionSamples) -> pd.DataFrame:
    return result.frame[result.frame["route"] == "closed_loop"].reset_index(drop=True)


def _empty(result: SessionSamples, route: str) -> str:
    kind = result.empty[route]
    assert kind is not None
    return kind


def _accounted(result: SessionSamples) -> None:
    frame = result.frame
    assert list(frame.columns) == list(COLUMNS)
    assert set(frame["state"]) <= set(STATES)
    assert frame.loc[frame["state"] == ELIGIBLE, "reason"].isna().all()
    assert frame.loc[frame["state"] != ELIGIBLE, "reason"].notna().all()
    for route, counts in result.counts.items():
        assert sum(counts[s] for s in STATES) == counts["total"], route


# -- the closed-loop route ------------------------------------------------------------


def test_a_steady_warm_cruise_is_eligible_with_the_bank_mean_total_trim(
    declaration: FuelCompDeclaration, delivery: LambdaDeliveryDeclaration, afr: AfrCalibration
) -> None:
    result = _run(_session(), declaration, delivery, afr)
    _accounted(result)
    closed = _closed(result)
    # No cut, no open loop: every candidate with a full steadiness dwell
    # behind it is eligible.
    eligible = closed[closed["state"] == ELIGIBLE]
    assert len(eligible) >= 110
    row = eligible.iloc[0]
    assert row["total_trim_b1"] == pytest.approx(1.5625 + 3.125)
    assert row["total_trim_b2"] == pytest.approx(-0.78125 + 2.34375)
    assert row["error_pct"] == pytest.approx((4.6875 + 1.5625) / 2)
    assert row["error_banks"] == "mean"
    # PID 0x11 byte 51 is 164..166 counts (ceil(51 x 3.21) .. ceil(52 x 3.21) - 1);
    # rpm 2000 is word 160.
    assert (row["throttle_counts_lo"], row["throttle_counts_hi"]) == (164, 166)
    assert row["rpm_word"] == 160
    assert row["byte_lo"] <= row["byte_hi"]
    assert row["purge_basis"] == "not_captured"
    assert result.empty["closed_loop"] is None
    assert result.revisions == ["synthetic"] and not result.straddles_flash


def test_the_total_trim_keeps_the_codebase_additive_definition() -> None:
    (b1,) = [s for s in SHARED_ANOMALY_SIGNALS if s.name == "derived:TOTAL_TRIM_B1"]
    (b2,) = [s for s in SHARED_ANOMALY_SIGNALS if s.name == "derived:TOTAL_TRIM_B2"]
    assert b1.input_groups == (("obd:SHORT_FUEL_TRIM_1",), ("obd:LONG_FUEL_TRIM_1",))
    assert b2.input_groups == (("obd:SHORT_FUEL_TRIM_2",), ("obd:LONG_FUEL_TRIM_2",))


def test_each_gate_names_itself(
    declaration: FuelCompDeclaration, delivery: LambdaDeliveryDeclaration, afr: AfrCalibration
) -> None:
    frame = _session(
        COOLANT_TEMP=lambda s: 60.0 if s < 10 else 90.0,
        BAROMETRIC_PRESSURE=lambda s: 70.0 if 100 <= s < 110 else 80.0,
        RPM=lambda s: 2000.0 + (300.0 if s % 2 else 0.0) if 80 <= s < 90 else 2000.0,
        COMMANDED_EQUIV_RATIO=lambda s: 0.0 if s == 40 else 1.0,
        FUEL_STATUS=lambda s: 4.0 if s in (40, 70) else 2.0,
    )
    result = _run(frame, declaration, delivery, afr)
    _accounted(result)
    closed = _closed(result)
    seconds = (closed["timestamp"] - pd.Timestamp(T0)).dt.total_seconds().astype(int)
    reason_at = dict(zip(seconds, closed["reason"], strict=True))

    def at(second: int) -> object:
        return reason_at[second]

    assert at(5) == "cold"
    assert at(45) == "post_fuel_cut"  # 15 s holdoff from lambda_delivery.yaml
    assert at(70) == "not_closed_loop"
    assert at(75) == "open_loop_recently"
    assert at(85) == "rpm_transient"
    assert at(105) == "baro_outside_band"
    assert result.counts["closed_loop"]["gated:cold"] >= 9


def test_a_read_outside_the_separation_is_unpaired_not_guessed(
    declaration: FuelCompDeclaration, delivery: LambdaDeliveryDeclaration, afr: AfrCalibration
) -> None:
    # Closed loop pairs within 500 ms (fuel_comp_fit.yaml). From 60 s bank 2's
    # LTFT is read every third second, so the other anchors are 1-2 s away
    # from any read.
    frame = _session(LONG_FUEL_TRIM_2=lambda s: None if s >= 60 and s % 3 else 2.34375)
    result = _run(frame, declaration, delivery, afr)
    closed = _closed(result)
    seconds = (closed["timestamp"] - pd.Timestamp(T0)).dt.total_seconds().astype(int)
    lonely = closed[(seconds >= 60) & (seconds % 3 != 0)]
    assert set(lonely["state"]) == {"unpaired"}
    assert set(lonely["reason"]) == {"obd:LONG_FUEL_TRIM_2"}
    assert (closed[(seconds >= 63) & (seconds % 3 == 0)]["state"] == ELIGIBLE).all()


def test_the_steadiness_window_reaches_forward_to_the_paired_read(
    declaration: FuelCompDeclaration, delivery: LambdaDeliveryDeclaration, afr: AfrCalibration
) -> None:
    """rpm is read 0.43 s after each trim anchor and steps at 59 s: the
    anchor at 59 pairs that read, so the step is inside its window."""
    frame = _session(RPM=lambda s: 2400.0 if s >= 59 else 2000.0, RPM_offset=0.45)
    closed = _closed(_run(frame, declaration, delivery, afr))
    seconds = (closed["timestamp"] - pd.Timestamp(T0)).dt.total_seconds().astype(int)
    reason = dict(zip(seconds, closed["reason"], strict=True))
    paired = dict(zip(seconds, closed["rpm"], strict=True))
    assert paired[59] == 2400.0
    assert reason[59] == "rpm_transient"  # [57.0, 59.45] holds 2000 and 2400
    assert pd.isna(reason[58])  # [56.0, 58.45]: steady, the step comes later
    sep = closed.set_index(seconds)["max_separation_s"].astype(float)
    assert sep[58] == pytest.approx(0.43, abs=1e-6)
    assert set(closed["pairing_max_separation_s"]) == {0.5}


def test_a_clock_that_cannot_carry_the_separation_says_so(
    declaration: FuelCompDeclaration, delivery: LambdaDeliveryDeclaration, afr: AfrCalibration
) -> None:
    frame = _session().drop(columns=["metadata_json"])  # no provenance: unknown basis
    closed = _closed(_run(frame, declaration, delivery, afr))
    assert set(closed["state"]) == {"unpaired"}
    assert set(closed["reason"]) == {"obd:SHORT_FUEL_TRIM_1 (clock: unknown)"}


@pytest.mark.parametrize(
    ("override", "empty"),
    [
        ({"BAROMETRIC_PRESSURE": None}, "never_captured: input_missing:obd:BAROMETRIC_PRESSURE"),
        ({"COOLANT_TEMP": 50.0}, "filtered: gated:cold"),
        ({"SHORT_FUEL_TRIM_1": None}, "never_captured: no obd:SHORT_FUEL_TRIM_1 reads"),
    ],
    ids=["input-never-captured", "gated", "anchor-never-captured"],
)
def test_never_captured_and_filtered_are_different_empties(
    declaration: FuelCompDeclaration,
    delivery: LambdaDeliveryDeclaration,
    afr: AfrCalibration,
    override: dict[str, object],
    empty: str,
) -> None:
    values: dict[str, Any] = {name: (lambda s, v=value: v) for name, value in override.items()}
    result = _run(_session(**values), declaration, delivery, afr)
    assert _empty(result, "closed_loop").startswith(empty)


def test_a_session_straddling_a_flash_splits_by_sample(
    declaration: FuelCompDeclaration, delivery: LambdaDeliveryDeclaration, afr: AfrCalibration
) -> None:
    flash = pd.Timestamp(T0) + pd.Timedelta(seconds=60)
    registry = _registry(
        ("before", datetime(2026, 8, 1, tzinfo=UTC)), ("after", flash.to_pydatetime())
    )
    result = _run(
        _session(),
        declaration,
        delivery,
        afr,
        registry=registry,
        resolver=_resolver({"after": "no_surviving_image"}),
    )
    _accounted(result)
    assert result.straddles_flash and result.revisions == ["after", "before"]
    assert result.refusals == {"after": "no_surviving_image"}
    closed = _closed(result)
    after = closed[closed["timestamp"] >= flash]
    assert set(after["reason"]) == {"rom_image:no_surviving_image"}
    assert set(after["state"]) == {"input_missing"}
    assert (closed[closed["timestamp"] < flash]["state"] == ELIGIBLE).any()


# -- the open-loop route: B06's rows through this path ------------------------------


@pytest.mark.parametrize(
    ("preference", "expected"),
    [
        # tests/test_lambda_delivery_known_answers.py's committed counts.
        (
            ["ram_final_target"],
            {
                "total": 1078,
                "fuel_cut": 151,
                "measured_rail": 19,
                "measured_missing": 1,
                "post_fuel_cut": 316,
                "target_transient": 31,
                ELIGIBLE: 560,
            },
        ),
        # tests/test_lambda_delivery_live.py's committed eligible count.
        (["mode01_pid_0x44"], {ELIGIBLE: 229}),
    ],
)
def test_the_b06_fixture_gives_its_committed_numbers_through_this_path(
    declaration: FuelCompDeclaration,
    delivery: LambdaDeliveryDeclaration,
    afr: AfrCalibration,
    preference: list[str],
    expected: dict[str, int],
) -> None:
    rules = declaration.samples.model_copy(
        update={
            "open_loop": declaration.samples.open_loop.model_copy(
                update={"commanded_preference": preference}
            )
        }
    )
    pinned = declaration.model_copy(update={"samples": rules})
    session = read_session(B06_SLICE, filters=[("source", "==", "obd")])
    result = _run(session, pinned, delivery, afr)
    _accounted(result)
    open_rows = result.frame[result.frame["route"] == "open_loop"]
    delivery_states = open_rows["delivery_state"].value_counts().to_dict()
    for state, count in expected.items():
        if state == "total":
            assert len(open_rows) == count
        else:
            assert delivery_states.get(state, 0) == count, state
    if preference == ["ram_final_target"]:
        enrichment = open_rows[
            (open_rows["delivery_state"] == ELIGIBLE) & (open_rows["target_lambda"] < 0.99)
        ]
        assert enrichment["error_b1_pct"].astype(float).to_numpy() == pytest.approx(
            [4.257, 3.163, 2.989, 3.149, 2.847, 2.864, 1.486, 2.403], abs=1e-3
        )
    # The slice holds no throttle input: every candidate says so, none is guessed.
    assert set(open_rows["state"]) == {"input_missing"}
    assert _empty(result, "open_loop").startswith("never_captured: input_missing:throttle")
    assert np.isnan(open_rows["byte_lo"].astype(float)).all()


def test_a_cell_that_depends_on_idle_air_or_purge_is_flagged_not_placed(
    declaration: FuelCompDeclaration, delivery: LambdaDeliveryDeclaration, afr: AfrCalibration
) -> None:
    """Near idle the unlogged inputs move the byte across columns; at a wide
    throttle they cannot. The flags say which input is responsible."""
    idle = _closed(
        _run(_session(RPM=750.0, THROTTLE_POS=20 * 100 / 255), declaration, delivery, afr)
    )
    row = idle[idle["state"] == ELIGIBLE].iloc[0]
    assert bool(row["cell_ambiguous"]) and bool(row["idle_air_moves_cell"])
    assert row["load_col_last"] - row["load_col_first"] > 1
    open_throttle = _closed(
        _run(_session(RPM=3000.0, THROTTLE_POS=200 * 100 / 255), declaration, delivery, afr)
    )
    row = open_throttle[open_throttle["state"] == ELIGIBLE].iloc[0]
    assert not bool(row["cell_ambiguous"])
    assert not bool(row["idle_air_moves_cell"]) and not bool(row["purge_moves_cell"])


# -- unresolvable, a refused image, the throttle-route split, bank 2 -------------------


def test_a_route_missing_only_the_image_is_unresolvable_not_never_captured(
    declaration: FuelCompDeclaration, delivery: LambdaDeliveryDeclaration, afr: AfrCalibration
) -> None:
    result = _run(
        _session(), declaration, delivery, afr, resolver=_resolver({"synthetic": "image_not_found"})
    )
    assert _empty(result, "closed_loop").startswith(
        "unresolvable: input_missing:rom_image:image_not_found"
    )
    # A channel the session never recorded is still never_captured.
    no_baro = _run(
        _session(BAROMETRIC_PRESSURE=None),
        declaration,
        delivery,
        afr,
        resolver=_resolver({"synthetic": "image_not_found"}),
    )
    assert _empty(no_baro, "closed_loop").startswith("never_captured: ")


def test_a_refused_image_marks_its_samples_instead_of_aborting(
    tmp_path: Path,
    declaration: FuelCompDeclaration,
    delivery: LambdaDeliveryDeclaration,
    afr: AfrCalibration,
) -> None:
    """The real resolver over an image whose low fill curve falls."""
    image = load_byte_image(fill_low=tuple(30000 - 1000 * i for i in range(16)))
    path = tmp_path / "falling.bin"
    path.write_bytes(image)
    registry = TuneRevisionRegistry(
        revisions=[
            TuneRevision(
                revision="falling",
                flashed_at=datetime(2026, 8, 1, tzinfo=UTC),
                image_sha256=hashlib.sha256(image).hexdigest(),
            )
        ]
    )
    resolver = RevisionImageResolver(
        registry,
        load_rom_map(PROFILE),
        declaration.rom,
        image_files=[path],
        slot=declaration.program.map_slot,
    )
    result = _run(_session(), declaration, delivery, afr, registry=registry, resolver=resolver)
    _accounted(result)
    assert result.refusals == {"falling": "curves_not_monotone"}
    closed = _closed(result)
    assert set(closed["reason"]) == {"rom_image:curves_not_monotone"}
    assert _empty(result, "closed_loop").startswith("unresolvable: ")


def test_eligible_samples_are_split_by_throttle_route(
    declaration: FuelCompDeclaration, delivery: LambdaDeliveryDeclaration, afr: AfrCalibration
) -> None:
    pid = _run(_session(), declaration, delivery, afr)
    split = eligible_summary(pid.frame)["synthetic"]["closed_loop"]
    assert split["by_throttle_route"]["pid_0x11"]["validated"] is True
    assert split["by_throttle_route"]["pid_0x11"]["eligible"] == split["eligible"] > 0
    # DID 0x120F only: warm idle for the closed-position estimate, then cruise.
    did = _run(
        _session(
            THROTTLE_POS=None,
            DID_120F=lambda s: 125 if s < 40 else 300,
            RPM=lambda s: 750.0 if s < 40 else 2000.0,
        ),
        declaration,
        delivery,
        afr,
    )
    assert did.closed_position is not None and did.closed_position["interval"] == [93, 102]
    route = eligible_summary(did.frame)["synthetic"]["closed_loop"]["by_throttle_route"]
    assert set(route) == {"did_0x120F"}
    assert route["did_0x120F"]["validated"] is False and route["did_0x120F"]["eligible"] > 0
    assert did.as_dict()["eligible"]["synthetic"]["closed_loop"]["by_throttle_route"] == route


def _enrichment(**overrides: Any) -> pd.DataFrame:
    """Steady open-loop enrichment at 4 Hz: PID 0x44 phi 1.15 (lambda 0.8696),
    bank 1 reading 2 % lean of it and bank 2 1 % lean."""
    target = 1 / 1.15
    channels: dict[str, Any] = {
        "RPM": 3000.0,
        "FUEL_STATUS": 4.0,
        "COMMANDED_EQUIV_RATIO": 1.15,
        "O2_WR_LAMBDA_S1": target * 1.02,
        "SHORT_FUEL_TRIM_1": None,
        "LONG_FUEL_TRIM_1": None,
        "SHORT_FUEL_TRIM_2": None,
        "LONG_FUEL_TRIM_2": None,
        "O2_WR_LAMBDA_S5": target * 1.01,
    }
    return _session(seconds=40, step=0.25, **{**channels, **overrides})


def test_bank_2_is_judged_by_its_declared_source_and_the_error_is_the_bank_mean(
    declaration: FuelCompDeclaration, delivery: LambdaDeliveryDeclaration, afr: AfrCalibration
) -> None:
    result = _run(_enrichment(), declaration, delivery, afr)
    _accounted(result)
    rows = result.frame[result.frame["route"] == "open_loop"]
    eligible = rows[rows["state"] == ELIGIBLE]
    assert len(eligible) > 50
    assert eligible["delivery_state_b2"].eq(ELIGIBLE).all()
    assert eligible["error_b1_pct"].astype(float).to_numpy() == pytest.approx(2.0, abs=1e-9)
    assert eligible["error_b2_pct"].astype(float).to_numpy() == pytest.approx(1.0, abs=1e-9)
    assert eligible["error_pct"].astype(float).to_numpy() == pytest.approx(1.5, abs=1e-9)
    assert set(eligible["error_banks"]) == {"mean"}
    bank1_only = _run(_enrichment(O2_WR_LAMBDA_S5=None), declaration, delivery, afr)
    rows = bank1_only.frame[bank1_only.frame["route"] == "open_loop"]
    eligible = rows[rows["state"] == ELIGIBLE]
    assert len(eligible) > 50 and set(eligible["error_banks"]) == {"b1"}
    assert eligible["error_pct"].astype(float).to_numpy() == pytest.approx(2.0, abs=1e-9)


# -- the open-loop route as an interval over the delay bracket (N02.5) ------------------


def _open(result: SessionSamples) -> pd.DataFrame:
    return result.frame[result.frame["route"] == "open_loop"].reset_index(drop=True)


def test_a_steady_enrichment_reads_one_rpm_word_across_the_delay_bracket(
    declaration: FuelCompDeclaration, delivery: LambdaDeliveryDeclaration, afr: AfrCalibration
) -> None:
    rows = _open(_run(_enrichment(THROTTLE_POS=200 * 100 / 255), declaration, delivery, afr))
    eligible = rows[rows["state"] == ELIGIBLE]
    assert len(eligible) > 50
    assert set(eligible["rpm_word_lo"]) == set(eligible["rpm_word_hi"]) == {240}
    assert not eligible["rpm_moves_cell"].astype(bool).any()
    # The bracket is lambda_delivery.yaml's, never restated.
    bracket = delivery.eligibility.regimes.enrichment.transport_delays[
        declaration.samples.open_loop.delay_bracket.delay_source
    ].bracket_s
    assert bracket == (0.0, 0.5)


def test_a_pull_is_placed_as_an_interval_not_gated_as_a_transient(
    declaration: FuelCompDeclaration, delivery: LambdaDeliveryDeclaration, afr: AfrCalibration
) -> None:
    """rpm climbs 2,000 rpm per second: the old 0.5 s / 100 rpm gate refused
    every row; now each row carries the bracket's rpm words and is marked
    ambiguous because they read more than one pair of Fuel Comp rows."""
    result = _run(
        _enrichment(RPM=lambda s: 2000.0 + 2000.0 * (s % 2.0), THROTTLE_POS=200 * 100 / 255),
        declaration,
        delivery,
        afr,
    )
    _accounted(result)
    rows = _open(result)
    # B06's own target dwell still applies; no rpm or throttle steadiness gate does.
    assert not {"rpm_transient", "throttle_transient"} & set(rows["reason"].dropna())
    assert not any("steadiness" in str(r) for r in rows["reason"])
    eligible = rows[rows["state"] == ELIGIBLE]
    assert len(eligible) > 20
    span = eligible["rpm_word_hi"].astype(int) - eligible["rpm_word_lo"].astype(int)
    assert (span >= 40).all()  # at least 500 rpm inside the 0.5 s bracket
    wide = eligible[
        eligible["rpm_row_last"].astype(int) - eligible["rpm_row_first"].astype(int) > 1
    ]
    assert len(wide) and wide["rpm_moves_cell"].astype(bool).all()
    assert wide["cell_ambiguous"].astype(bool).all()


def test_a_bracket_without_a_bounding_read_is_unpaired_and_says_so(
    declaration: FuelCompDeclaration, delivery: LambdaDeliveryDeclaration, afr: AfrCalibration
) -> None:
    """A 0.75 s hole in the rpm reads: the read at the target instant pairs,
    but nothing bounds the start of the delay bracket within 0.25 s."""
    result = _run(
        _enrichment(RPM=lambda s: None if (s % 2.0) in (1.0, 1.25) else 3000.0),
        declaration,
        delivery,
        afr,
    )
    _accounted(result)
    reasons = set(_open(result)["reason"].dropna())
    assert "obd:RPM (delay bracket)" in reasons


def test_maf_and_bfs_ride_along_without_gating(
    declaration: FuelCompDeclaration, delivery: LambdaDeliveryDeclaration, afr: AfrCalibration
) -> None:
    result = _run(_session(MAF=55.0, DID_1208=20000), declaration, delivery, afr)
    _accounted(result)
    eligible = _closed(result)
    eligible = eligible[eligible["state"] == ELIGIBLE]
    assert len(eligible) > 100
    assert set(eligible["maf_gs"].astype(float)) == {55.0}
    assert set(eligible["bfs_raw"].astype(float)) == {20000.0}
    # Not captured is NaN, never a gate.
    plain = _closed(_run(_session(), declaration, delivery, afr))
    assert plain["maf_gs"].astype(float).isna().all()
    assert (plain["state"] == ELIGIBLE).sum() == len(eligible)


def test_a_bracket_that_holds_a_stopped_engine_is_gated_not_placed(
    declaration: FuelCompDeclaration, delivery: LambdaDeliveryDeclaration, afr: AfrCalibration
) -> None:
    """rpm 0 inside the span: the chain's divide guard (w116A = 0 at word 0)
    breaks the byte's monotonicity in rpm, so no interval exists
    (_capture-tuning-2026-09-22T235224 raised on it before this gate)."""
    result = _run(
        _enrichment(RPM=lambda s: 0.0 if 10.0 <= s < 10.5 else 3000.0),
        declaration,
        delivery,
        afr,
    )
    _accounted(result)
    reasons = set(_open(result)["reason"].dropna())
    assert "engine_stopped_in_delay_bracket" in reasons


def test_the_ram_target_is_preferred_and_pid_0x44_carries_its_caveat(
    declaration: FuelCompDeclaration, delivery: LambdaDeliveryDeclaration, afr: AfrCalibration
) -> None:
    """PID 0x44 is the phi word times the A/F feedback target / 3675
    (firmware, 2026-10-05): phi only while that target is 14.7, which history
    never logged. So the RAM word leads, and PID 0x44 must declare why it is
    weaker wherever it is still used."""
    rules = declaration.samples.open_loop
    assert rules.commanded_preference[0] == "ram_final_target"
    assert "mode01_pid_0x44" in rules.source_caveats
    assert "46B2" in rules.source_caveats["mode01_pid_0x44"]
    # A PID-0x44-only session still uses it, labelled.
    rows = _open(_run(_enrichment(), declaration, delivery, afr))
    assert set(rows["commanded_source"].dropna()) == {"mode01_pid_0x44"}
    with pytest.raises(ValueError, match="source_caveats"):
        type(rules).model_validate({**rules.model_dump(), "source_caveats": {"nope": "x"}})
