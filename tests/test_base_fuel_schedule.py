"""P02.14: DID 0x1208 in ms is the Xterra's target-map load axis.

Operator decision 2026-10-02: accept ``did:ECM_AIR_CHARGE`` as the verified
base fuel schedule (raw x 0.00049 reproduces UpRev's displayed BFS on 405/405
rows, 2026-09-20). The deployed IF and LSTM were trained on its raw counts, so
the DID keeps decoding as it did. The ms reading is a separate channel,
``derived:BASE_FUEL_SCHEDULE``, scaled by the one ``dids.yaml`` evidence
record that established the constant. These tests pin:

* the DID still decodes to raw counts (no served feature value moves);
* the view's scale and unit come from that evidence record, once, and the
  tuning workbench's own provider agrees with it, so BFS is read in ms
  exactly once on both paths;
* a source scaled in place, or a missing or ambiguous record, refuses the
  view instead of scaling twice or guessing;
* on a real drive slice, ``derived:TARGET_AFR`` is the target map's bilinear
  lookup on BFS-in-ms x rpm, and BFS sits on the map's axis there.

The slice: ``tests/fixtures/base_fuel_schedule/_capture-tuning-2026-09-23T164007``,
2026-09-23 16:43:00-16:45:00 UTC of that capture, Mode 01 RPM, coolant, load,
fuel status and wide-range lambda plus DIDs 0x1208/0x1225/0x1226, rows copied
unchanged. It is on calibration ZS44A-20260920 (flashed 09-21 03:08Z), the
map ``afr_calibration.yaml`` marks active, and it holds a pull to 5,475 rpm,
so the lookup leaves the all-14.7 cells.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from src.analysis.tuning_frame import build_tuning_frame, load_provider_registry
from src.config import CanaryConfig
from src.decode.dids import DidMap, decode_raw_did_reading, did_entries_by_hex, load_did_map
from src.features.feature_pipeline import (
    DID_VIEW_EXCLUSION,
    FeaturePolicy,
    build_feature_frame,
    featurize_wide_frame,
    load_resampled_session,
)
from src.paths import DIDS_YAML
from src.preprocessing.channel_units import (
    append_did_views,
    catalog_channel_unit,
    channel_units_for,
    did_view_refusal,
    did_view_scale,
)
from src.signals import vehicle_signals
from src.signals.afr import (
    DEFAULT_TARGET_LOAD_AXIS,
    TARGET_AFR,
    TARGET_AFR_CHANNELS,
    TargetAfrMap,
    load_afr_calibration,
    load_axis_refusal,
)
from src.signals.derived import DERIVED_CHANNEL_INPUTS
from src.signals.vehicle_signals import DidView, did_views, load_axis_columns
from src.signals.vehicle_signals.nissan_xterra_2008 import BASE_FUEL_SCHEDULE
from src.vehicle.profiles import resolve_profile_for_tooling, scope_config
from src.yaml_load import load_yaml
from tests.configs import EXAMPLE_YAML

SLICE = Path(__file__).parent / "fixtures/base_fuel_schedule/_capture-tuning-2026-09-23T164007"
AFR_CALIBRATION = Path("data/nissan-xterra-2008/afr_calibration.yaml")
PROVIDERS = Path("data/nissan-xterra-2008/tuning_providers.yaml")
#: The constant the 405/405 identity established, ms per raw count.
MS_PER_COUNT = 0.00049
#: The repo DID catalog: what ``load_resampled_session`` decodes the slice with.
REPO_DIDS = load_did_map(DIDS_YAML)
POLICY = FeaturePolicy(
    bindings=None, serving_dids=frozenset(), serving_mode01=frozenset({"AUTO"}), did_map=REPO_DIDS
)


def _view() -> DidView:
    (view,) = [v for v in did_views() if v.channel == BASE_FUEL_SCHEDULE]
    return view


def _scoped() -> CanaryConfig:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    return scope_config(config, resolve_profile_for_tooling(config, None))


# ---------------------------------------------------------------------------
# The DID itself is untouched: served feature values do not move
# ---------------------------------------------------------------------------


def test_air_charge_still_decodes_to_raw_counts() -> None:
    """``did:ECM_AIR_CHARGE`` is an IF and LSTM feature. A scale on it would
    change what those models are served, which is a retrain."""
    catalog = load_did_map(DIDS_YAML)
    (entry,) = [e for e in catalog.dids if e.did == 0x1208]
    assert entry.name == "ECM_AIR_CHARGE"
    assert entry.scale is None and entry.offset is None and entry.unit == "unknown"
    hit = decode_raw_did_reading(did_entries_by_hex(catalog), "DID_1208", "6c5c", source="obd")
    assert hit is not None
    assert (hit.value, hit.unit) == (float(0x6C5C), "raw")
    assert catalog_channel_unit("did:ECM_AIR_CHARGE", did_map=REPO_DIDS) == "unknown"


# ---------------------------------------------------------------------------
# One scale, from one evidence record, read once on every path
# ---------------------------------------------------------------------------


def test_the_view_is_scaled_by_the_0x1208_evidence_record() -> None:
    view = _view()
    assert view.source == "did:ECM_AIR_CHARGE"
    scaled = did_view_scale(view, did_map=REPO_DIDS)
    assert scaled is not None
    assert scaled.scale == MS_PER_COUNT
    assert scaled.unit == "ms"
    assert catalog_channel_unit(BASE_FUEL_SCHEDULE, did_map=REPO_DIDS) == "ms"
    # The refuted 2048 counts/ms must not creep back through the view.
    assert scaled.scale != pytest.approx(1 / 2048, rel=1e-4)


def test_the_workbench_provider_and_the_view_agree_and_both_scale_raw_counts() -> None:
    """The tuning workbench scales the same DID itself (``tuning_providers.yaml``).
    Both must multiply raw counts by the same constant: a scale on the DID in
    ``dids.yaml`` would make both read BFS scaled twice, and a drifted factor
    would make the digest and ``TARGET_AFR`` disagree about one reading."""
    registry = load_provider_registry(PROVIDERS)
    (bfs,) = [p for p in registry.providers if p.role == DEFAULT_TARGET_LOAD_AXIS]
    scaled = did_view_scale(_view(), did_map=REPO_DIDS)
    assert scaled is not None
    assert (bfs.spec, bfs.transform, bfs.unit) == (_view().source, "scale", scaled.unit)
    assert bfs.factor == scaled.scale
    assert catalog_channel_unit(bfs.spec, did_map=REPO_DIDS) == "unknown", (
        "the workbench's input is raw counts"
    )


def test_the_workbench_and_featurization_read_the_same_ms_on_a_real_slice() -> None:
    """End to end on the slice: the workbench's ``base_fuel_schedule`` role and
    the featurized ``derived:BASE_FUEL_SCHEDULE`` give the same ms for the same
    raw reading, and both are raw x 0.00049."""
    frame = build_tuning_frame(
        SLICE,
        providers_path=PROVIDERS,
        calibration=load_afr_calibration(AFR_CALIBRATION),
    )
    workbench = frame.values.dropna(subset=["base_fuel_schedule"])
    assert len(workbench) > 100
    np.testing.assert_allclose(
        workbench["base_fuel_schedule"], workbench["base_fuel_schedule_raw"] * MS_PER_COUNT
    )
    by_raw = dict(
        zip(workbench["base_fuel_schedule_raw"], workbench["base_fuel_schedule"], strict=True)
    )
    operating = featurize_wide_frame(
        load_resampled_session(SLICE, _scoped()),
        _scoped(),
        behavior_models=None,
        did_map=REPO_DIDS,
    ).operating_frame
    pairs = operating[["did:ECM_AIR_CHARGE", BASE_FUEL_SCHEDULE]].dropna()
    matched = [(by_raw[raw], ms) for raw, ms in pairs.itertuples(index=False) if raw in by_raw]
    assert len(matched) > 50
    raws, schedules = zip(*matched, strict=True)
    np.testing.assert_allclose(raws, schedules)


# ---------------------------------------------------------------------------
# Refusals: no double scaling, no guessed scale
# ---------------------------------------------------------------------------


def _edited_catalog(tmp_path: Path, edit: dict[str, object]) -> Path:
    document = load_yaml(DIDS_YAML.read_text())
    (entry,) = [e for e in document["dids"] if e["did"] == 0x1208]
    for key, value in edit.items():
        if key == "evidence_method":
            for record in entry["evidence"]:
                if record["method"] == _view().scale_method:
                    record["method"] = value
        else:
            entry[key] = value
    path = tmp_path / "dids.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    return path


def _air_charge_frame() -> pd.DataFrame:
    """The last row is a held (stale) value: finite, but ``__gap`` True."""
    return pd.DataFrame(
        {
            "did:ECM_AIR_CHARGE": [6000.0, 20400.0, np.nan, 40800.0, 40800.0],
            "did:ECM_AIR_CHARGE__gap": [False, False, True, False, True],
            "did:ECM_AIR_CHARGE__arrived": [True, False, False, True, True],
        }
    )


def test_a_source_scaled_in_place_refuses_the_view(tmp_path: Path) -> None:
    """If 0x1208 is ever scaled in ``dids.yaml``, its frame column is in ms
    already and the view would scale it twice. It is refused, the catalog
    gives it no unit, and the load axis is refused with it."""
    scaled = load_did_map(_edited_catalog(tmp_path, {"scale": MS_PER_COUNT, "unit": "ms"}))
    reason = did_view_refusal(_view(), did_map=scaled)
    assert reason is not None and "twice" in reason
    assert did_view_scale(_view(), did_map=scaled) is None
    assert catalog_channel_unit(BASE_FUEL_SCHEDULE, did_map=scaled) is None
    assert BASE_FUEL_SCHEDULE not in append_did_views(_air_charge_frame(), did_map=scaled)


@pytest.mark.parametrize("method", ["renamed_method", None])
def test_the_scale_must_come_from_exactly_one_named_record(
    tmp_path: Path, method: str | None
) -> None:
    """No record under the method: refused. Two: refused, rather than one
    picked by list order."""
    if method is None:
        document = load_yaml(DIDS_YAML.read_text())
        (entry,) = [e for e in document["dids"] if e["did"] == 0x1208]
        (record,) = [r for r in entry["evidence"] if r["method"] == _view().scale_method]
        entry["evidence"].append({**record, "constant": 1 / 2048})
        path = tmp_path / "dids.yaml"
        path.write_text(yaml.safe_dump(document, sort_keys=False))
        expected = "has 2 evidence records"
    else:
        path = _edited_catalog(tmp_path, {"evidence_method": method})
        expected = "has 0 evidence records"
    catalog = load_did_map(path)
    reason = did_view_refusal(_view(), did_map=catalog)
    assert reason is not None and expected in reason
    assert catalog_channel_unit(BASE_FUEL_SCHEDULE, did_map=catalog) is None


def test_the_view_is_the_source_times_the_scale_with_its_freshness() -> None:
    """The gap is the source's own flag, not just "value missing": the last
    row holds a finite value the source marks stale, and the view is stale
    there too (and so not arrived)."""
    out = append_did_views(_air_charge_frame(), did_map=REPO_DIDS)
    np.testing.assert_allclose(
        out[BASE_FUEL_SCHEDULE],
        [6000 * MS_PER_COUNT, 20400 * MS_PER_COUNT, np.nan, 40800 * MS_PER_COUNT]
        + [40800 * MS_PER_COUNT],
    )
    assert list(out[f"{BASE_FUEL_SCHEDULE}__gap"]) == [False, False, True, False, True]
    assert list(out[f"{BASE_FUEL_SCHEDULE}__arrived"]) == [True, False, False, True, False]
    unchanged = pd.DataFrame({"obd:RPM": [800.0]})
    assert append_did_views(unchanged, did_map=REPO_DIDS) is unchanged


# ---------------------------------------------------------------------------
# The catalog that decoded the frame is the one that judges it (verifier)
# ---------------------------------------------------------------------------


def _policy(did_map: DidMap | None) -> FeaturePolicy:
    return FeaturePolicy(
        bindings=None, serving_dids=frozenset(), serving_mode01=frozenset({"AUTO"}), did_map=did_map
    )


def test_a_frame_decoded_with_a_scaled_catalog_gets_no_view_and_no_target(
    tmp_path: Path,
) -> None:
    """P02.14 verification, MEDIUM. A frame decoded with a catalog that scales
    0x1208 to ms holds ms in ``did:ECM_AIR_CHARGE``. The guards used to read the
    repo file instead, which says raw, so the view scaled the ms again (max
    0.0088) and TARGET_AFR went empty, silently. Through the real build path
    (``build_feature_frame(dids_path=)``) the view is now refused, the target is
    absent rather than looked up on a doubly scaled axis, and the policy built
    on the same catalog names the refusal."""
    path = _edited_catalog(tmp_path, {"scale": MS_PER_COUNT, "unit": "ms"})
    features = build_feature_frame([SLICE], _scoped(), dids_path=path)
    air = features["did:ECM_AIR_CHARGE"].dropna()
    assert len(air) > 100 and air.max() < 25.0, "the scaled catalog decoded the DID to ms"
    assert BASE_FUEL_SCHEDULE not in features.columns
    for channel in TARGET_AFR_CHANNELS:
        assert channel not in features.columns
    standing = _policy(load_did_map(path)).standing_exclusions()
    assert standing[TARGET_AFR].startswith("load_axis_unverified")
    assert BASE_FUEL_SCHEDULE in standing[TARGET_AFR]
    # The same slice through the repo catalog: the view and the target exist.
    repo = build_feature_frame([SLICE], _scoped(), dids_path=DIDS_YAML)
    assert repo[BASE_FUEL_SCHEDULE].max() == pytest.approx(
        repo["did:ECM_AIR_CHARGE"].max() * MS_PER_COUNT
    )
    assert repo[TARGET_AFR].notna().sum() >= 40


def test_no_decoding_catalog_means_no_view_and_a_named_refusal() -> None:
    reason = did_view_refusal(_view(), did_map=None)
    assert reason is not None and "no DID catalog decoded this frame" in reason
    assert catalog_channel_unit(BASE_FUEL_SCHEDULE, did_map=None) is None
    assert catalog_channel_unit("did:ECM_AIR_CHARGE", did_map=None) is None
    assert BASE_FUEL_SCHEDULE not in append_did_views(_air_charge_frame(), did_map=None)
    assert TARGET_AFR in _policy(None).standing_exclusions()
    operating = featurize_wide_frame(
        load_resampled_session(SLICE, _scoped()), _scoped(), behavior_models=None, did_map=None
    ).operating_frame
    assert BASE_FUEL_SCHEDULE not in operating.columns
    assert TARGET_AFR not in operating.columns


def test_featurization_has_no_default_catalog() -> None:
    """``did_map`` is required, like ``behavior_models``: a default would be
    the repo file, which is the mismatch."""
    import dataclasses
    import inspect

    from src.features.feature_pipeline import featurize_perturbed_frame

    for function in (featurize_wide_frame, featurize_perturbed_frame):
        parameter = inspect.signature(function).parameters["did_map"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty, function.__name__
    field = {f.name: f for f in dataclasses.fields(FeaturePolicy)}["did_map"]
    assert field.default is dataclasses.MISSING


@pytest.mark.parametrize(
    ("channel", "source", "method", "match"),
    [
        ("BASE_FUEL_SCHEDULE", "did:ECM_AIR_CHARGE", "m", "derived:"),
        ("derived:X", "mem:ECM_ENGINE_PERIOD", "m", "not a did:"),
        ("derived:X", "did:ECM_AIR_CHARGE", " ", "evidence method"),
    ],
)
def test_a_view_declaration_is_checked(channel: str, source: str, method: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        DidView(channel=channel, source=source, scale_method=method)


def test_two_vehicles_cannot_declare_one_view_name_differently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import types

    other = types.SimpleNamespace(
        DID_VIEWS=(DidView(BASE_FUEL_SCHEDULE, "did:ECM_INJECTOR_PULSE_1", "x"),)
    )
    real = vehicle_signals.vehicle_modules()
    monkeypatch.setattr(vehicle_signals, "vehicle_modules", lambda: (*real, other))
    with pytest.raises(ValueError, match="two different DID views"):
        did_views()


# ---------------------------------------------------------------------------
# The load axis and the feature policy
# ---------------------------------------------------------------------------


def test_the_xterra_declares_the_view_as_its_verified_load_axis() -> None:
    assert load_axis_columns(DEFAULT_TARGET_LOAD_AXIS) == (BASE_FUEL_SCHEDULE,)
    assert DERIVED_CHANNEL_INPUTS[TARGET_AFR] == (("obd:RPM",), (BASE_FUEL_SCHEDULE,))
    assert DERIVED_CHANNEL_INPUTS[BASE_FUEL_SCHEDULE] == (("did:ECM_AIR_CHARGE",),)
    assert load_axis_refusal(channel_units=channel_units_for(REPO_DIDS)) is None


def test_the_target_exclusion_lifts_and_the_view_is_refused_as_a_duplicate() -> None:
    """P02.13's standing exclusion lifts by itself. The view is the source DID
    times a constant, so it never becomes a second copy of one feature."""
    standing = POLICY.standing_exclusions()
    assert not set(standing) & set(TARGET_AFR_CHANNELS)
    assert standing[BASE_FUEL_SCHEDULE].startswith(DID_VIEW_EXCLUSION)
    reason = POLICY.exclusion_reason(BASE_FUEL_SCHEDULE)
    assert reason is not None and reason.startswith(DID_VIEW_EXCLUSION)
    for channel in TARGET_AFR_CHANNELS:
        reason = POLICY.exclusion_reason(channel)
        assert reason is None or not reason.startswith("load_axis_unverified"), channel


# ---------------------------------------------------------------------------
# Known answer on a real drive slice
# ---------------------------------------------------------------------------


def _bilinear(target: TargetAfrMap, rpm: float, load: float) -> float:
    """The four-cell bilinear formula, written out independently of
    ``src.signals.afr.target_afr``. NaN off the map, as the lookup does, except
    that a load below the first breakpoint is the first column, and an rpm
    above the last the last row, where the map declares the ECM's clamp
    (P02.15)."""
    rpms = list(target.rpm_breakpoints)
    loads = list(target.load_breakpoints)
    if target.clamps_below_first_load and load < loads[0]:
        load = loads[0]
    if target.clamps_above_last_rpm and rpm > rpms[-1]:
        rpm = rpms[-1]
    if not (rpms[0] <= rpm <= rpms[-1] and loads[0] <= load <= loads[-1]):
        return float("nan")
    i = max(k for k in range(len(rpms) - 1) if rpms[k] <= rpm)
    j = max(k for k in range(len(loads) - 1) if loads[k] <= load)
    u = (rpm - rpms[i]) / (rpms[i + 1] - rpms[i])
    v = (load - loads[j]) / (loads[j + 1] - loads[j])
    cell = target.afr
    return (
        cell[i][j] * (1 - u) * (1 - v)
        + cell[i + 1][j] * u * (1 - v)
        + cell[i][j + 1] * (1 - u) * v
        + cell[i + 1][j + 1] * u * v
    )


@pytest.fixture(scope="module")
def slice_operating() -> pd.DataFrame:
    scoped = _scoped()
    wide = load_resampled_session(SLICE, scoped)
    return featurize_wide_frame(
        wide, scoped, behavior_models=None, did_map=REPO_DIDS
    ).operating_frame


def test_target_afr_is_the_maps_lookup_on_bfs_in_ms(slice_operating: pd.DataFrame) -> None:
    calibration = load_afr_calibration(AFR_CALIBRATION)
    # The slice was recorded 2026-09-23, on the 2026-09-20 flash (P02.16).
    target = calibration.target_map("ZS44A-20260920")
    assert target is not None and target.clamps_below_first_load
    rows = slice_operating[["obd:RPM", "did:ECM_AIR_CHARGE", TARGET_AFR]]
    expected = np.array(
        [
            _bilinear(target, rpm, raw * MS_PER_COUNT)
            for rpm, raw in zip(rows["obd:RPM"], rows["did:ECM_AIR_CHARGE"], strict=True)
        ]
    )
    np.testing.assert_allclose(rows[TARGET_AFR].to_numpy(dtype=float), expected, rtol=1e-9)
    inside = np.isfinite(expected)
    # Not vacuous: the pull leaves the closed-loop (14.7) cells.
    assert inside.sum() >= 40
    assert (expected[inside] < 14.69).sum() >= 15


def test_bfs_in_ms_lands_on_the_maps_axis_where_the_map_applies(
    slice_operating: pd.DataFrame,
) -> None:
    """Zero scaling would put BFS in the thousands (raw counts), and scaling
    twice near 1e-3 ms: neither reaches the 8.76-21.57 ms axis. Measured
    here: 1.6-18.0 ms, 1.6-2 ms on the over-run and idle rows."""
    bfs = slice_operating[BASE_FUEL_SCHEDULE].dropna()
    assert len(bfs) > 100
    assert 1.0 < bfs.min() and bfs.max() < 25.0
    target = load_afr_calibration(AFR_CALIBRATION).target_map("ZS44A-20260920")
    assert target is not None
    high_load = bfs[slice_operating.loc[bfs.index, "obd:RPM"] >= target.rpm_breakpoints[3]]
    on_axis = high_load.between(target.load_breakpoints[0], target.load_breakpoints[-1])
    assert on_axis.mean() > 0.4


def test_afr_error_comes_back_as_measured_minus_target(slice_operating: pd.DataFrame) -> None:
    """``AFR_ERROR_B*`` return with the target, as measured minus target on
    uncensored rows. Their size is not evidence about the axis: the median
    bank-1 error on this slice is +0.05 AFR at the right scale and +0.03 on a
    1.48x axis, because most rows sit in 14.7 cells either way."""
    for bank in (1, 2):
        error = slice_operating[f"derived:AFR_ERROR_B{bank}"]
        measured = slice_operating[f"derived:AFR_B{bank}"]
        censored = slice_operating[f"derived:AFR_CENSORED_B{bank}"].astype(bool)
        expected = (measured - slice_operating[TARGET_AFR]).where(~censored)
        pd.testing.assert_series_equal(error, expected, check_names=False)
        assert error.notna().sum() >= 30
