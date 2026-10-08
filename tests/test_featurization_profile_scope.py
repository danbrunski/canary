"""P02.11: featurization reads the vehicle's data through the profile-scoped config.

The daemon scopes its config in ``main`` and so applied the vehicle's
``afr_calibration.yaml``; the trainers and the evaluation harness featurized
through the unscoped config, where ``data/afr_calibration.yaml`` does not
exist, so every offline frame lacked the calibration the served one had.
Featurization now refuses an unscoped config while profiles are enabled, and
every offline entry point scopes once.
"""

from __future__ import annotations

import pickle
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.config import CanaryConfig, UnscopedConfigError
from src.decode.dids import load_repo_did_map
from src.features.feature_cache import build_feature_frame_cache_key
from src.features.feature_pipeline import (
    build_feature_frame,
    featurize_perturbed_frame,
    featurize_wide_frame,
)
from src.features.live import LiveFeatureSource
from src.models.sequence_feature_set import sequence_roles_path
from src.signals.afr import BaroSamples, load_afr_calibration, voltage_to_afr
from src.vehicle.profiles import resolve_profile_for_tooling, scope_config
from tests.configs import EXAMPLE_YAML, profiles_disabled

SLUG = "nissan-xterra-2008"


def _unscoped() -> CanaryConfig:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    assert config.profiles.enabled, "the example config runs with profiles on"
    return config


def _scoped() -> CanaryConfig:
    config = _unscoped()
    return scope_config(config, resolve_profile_for_tooling(config, None))


def _wide(rows: int = 40) -> pd.DataFrame:
    """Warm idle-to-cruise rows whose rpm and load sit inside the target map."""
    rng = np.random.default_rng(3)
    return pd.DataFrame(
        {
            "session": "2026-09-20T152758",
            "timestamp": pd.date_range("2026-09-20 15:30", periods=rows, freq="1s", tz="UTC"),
            "obd:RPM": np.linspace(1300.0, 3500.0, rows),
            "obd:ENGINE_LOAD": np.linspace(9.0, 21.0, rows),
            "obd:SPEED": np.linspace(20.0, 60.0, rows),
            "obd:COOLANT_TEMP": 88.0,
            "obd:THROTTLE_POS": 15.0 + rng.normal(0.0, 0.5, rows),
        }
    )


# ---------------------------------------------------------------------------
# The marker
# ---------------------------------------------------------------------------


def test_scope_config_records_the_slug_and_is_idempotent() -> None:
    base = _unscoped()
    assert base.scoped_profile is None
    scoped = _scoped()
    assert scoped.scoped_profile == SLUG
    assert scoped.daemon.afr_calibration == base.daemon.afr_calibration.parent / SLUG / (
        base.daemon.afr_calibration.name
    )
    # Re-scoping is a no-op, never <slug>/<slug>/.
    assert scope_config(scoped, resolve_profile_for_tooling(scoped, None)) is scoped
    assert resolve_profile_for_tooling(scoped, None).slug == SLUG
    with pytest.raises(ValueError, match="already scoped"):
        scope_config(scoped, resolve_profile_for_tooling(base, "another-vehicle"))


def test_the_marker_survives_copies_and_the_process_boundary() -> None:
    """The inference and digest children get the parent's object pickled."""
    scoped = _scoped()
    assert pickle.loads(pickle.dumps(scoped)).scoped_profile == SLUG
    assert scoped.model_copy(update={"daemon": scoped.daemon}).scoped_profile == SLUG


def test_rescope_like_reuses_the_resolved_profile_not_a_rederived_one() -> None:
    """The hot reload's re-scope: the slug *and* the registry parameters the
    daemon resolved, which ``resolve_profile_for_tooling`` would not reproduce."""
    from src.vehicle.profiles import rescope_like
    from tests.configs import TEST_PROFILE, scoped_test_config

    running = scoped_test_config(_unscoped())
    fresh = _unscoped()
    again = rescope_like(fresh, running)
    assert again.scoped_profile == TEST_PROFILE.slug
    assert again.vehicle.parameters == TEST_PROFILE.parameters != fresh.vehicle.parameters
    assert again.daemon.afr_calibration == running.daemon.afr_calibration
    assert resolve_profile_for_tooling(fresh, None).slug != TEST_PROFILE.slug
    # Carried across the process boundary with the config.
    assert rescope_like(fresh, pickle.loads(pickle.dumps(running))) == again
    # A profile-less daemon's reload stays as loaded.
    off = profiles_disabled(_unscoped())
    assert rescope_like(off, off) is off


def test_yaml_cannot_claim_to_be_scoped() -> None:
    data = _unscoped().model_dump(mode="json")
    data["_scoped_profile"] = SLUG
    with pytest.raises(ValueError):
        CanaryConfig.model_validate(data)


# ---------------------------------------------------------------------------
# The refusal
# ---------------------------------------------------------------------------


def test_featurize_wide_frame_refuses_an_unscoped_config_and_names_the_incident() -> None:
    with pytest.raises(UnscopedConfigError) as excinfo:
        featurize_wide_frame(
            _wide(), _unscoped(), behavior_models=None, did_map=load_repo_did_map()
        )
    message = str(excinfo.value)
    assert "P02.11" in message
    assert "afr_calibration" in message
    assert "trainers" in message and "evaluation harness" in message


def test_every_featurization_entry_refuses_an_unscoped_config(tmp_path: Path) -> None:
    unscoped = _unscoped()
    clean_wide = _wide()
    clean = featurize_wide_frame(
        clean_wide, _scoped(), behavior_models=None, did_map=load_repo_did_map()
    )
    with pytest.raises(UnscopedConfigError):
        featurize_perturbed_frame(
            clean,
            clean_wide,
            clean_wide.copy(),
            unscoped,
            behavior_models=None,
            did_map=load_repo_did_map(),
        )
    with pytest.raises(UnscopedConfigError):
        LiveFeatureSource(config=unscoped)
    with pytest.raises(UnscopedConfigError):
        build_feature_frame_cache_key([tmp_path / "2026-09-20T152758"], unscoped)
    with pytest.raises(UnscopedConfigError):
        sequence_roles_path(unscoped)


def test_build_feature_frame_refuses_before_the_cache(tmp_path: Path) -> None:
    """A cache hit must not hand back a frame an unscoped config could never
    have built correctly: the refusal comes before the lookup (and before the
    sessions are even read)."""
    with pytest.raises(UnscopedConfigError):
        build_feature_frame([tmp_path / "missing"], _unscoped(), cache_dir=tmp_path / "cache")


def test_scoped_and_profile_less_configs_featurize() -> None:
    assert not featurize_wide_frame(
        _wide(), _scoped(), behavior_models=None, did_map=load_repo_did_map()
    ).features.empty
    assert not featurize_wide_frame(
        _wide(), profiles_disabled(_unscoped()), behavior_models=None, did_map=load_repo_did_map()
    ).features.empty
    assert LiveFeatureSource(config=_scoped()) is not None
    assert LiveFeatureSource(config=profiles_disabled(_unscoped())) is not None


# ---------------------------------------------------------------------------
# Known answer: the calibration reaches offline featurization
# ---------------------------------------------------------------------------


def test_the_calibration_reaches_offline_featurization_for_a_calibrated_profile() -> None:
    """The A/F sensor voltage becomes ``derived:AFR_B1`` only through the
    calibration's transfer. Through the scoped config it is the vehicle's own
    transfer, applied exactly; the old unscoped read
    (``data/afr_calibration.yaml``, absent) produced none.

    ``derived:TARGET_AFR`` was this test's known answer until P02.13. It is
    absent on both paths here: the lookup no longer reads calculated load, and
    this frame lacks the Xterra's verified axis, 0x1208 in ms (P02.14; its
    known answer is ``tests/test_base_fuel_schedule.py``)."""
    scoped = _scoped()
    assert scoped.daemon.afr_calibration.is_file()
    # B06.7: the voltage route applies the ECM's barometric gain, from the
    # baro read as of each row; 80 kPa is a real, non-unity gain.
    wide = _wide().assign(
        **{
            "did:ECM_AF_SENSOR_V_B1": np.linspace(1.0, 2.5, 40),
            "obd:BAROMETRIC_PRESSURE": 80.0,
            "obd:BAROMETRIC_PRESSURE__arrived": True,
        }
    )
    operating = featurize_wide_frame(
        wide, scoped, behavior_models=None, did_map=load_repo_did_map()
    ).operating_frame
    calibration = load_afr_calibration(scoped.daemon.afr_calibration)
    assert calibration.voltage_transfer is not None
    baro = BaroSamples(np.full(len(wide), 80.0), np.zeros(len(wide)))
    expected = voltage_to_afr(wide["did:ECM_AF_SENSOR_V_B1"], calibration, baro=baro).afr
    assert expected.notna().all(), "the fixture voltages sit inside the transfer"
    pd.testing.assert_series_equal(
        operating["derived:AFR_B1"].reset_index(drop=True),
        expected.reset_index(drop=True),
        check_names=False,
    )
    assert "derived:TARGET_AFR" not in operating.columns
    old = profiles_disabled(_unscoped())
    assert not old.daemon.afr_calibration.exists()
    old_operating = featurize_wide_frame(
        wide, old, behavior_models=None, did_map=load_repo_did_map()
    ).operating_frame
    assert "derived:AFR_B1" not in old_operating.columns
    assert "derived:TARGET_AFR" not in old_operating.columns


def test_the_feature_cache_key_hashes_the_calibration_featurization_reads(
    tmp_path: Path,
) -> None:
    session = tmp_path / "2026-09-20T152758"
    session.mkdir()
    afr = tmp_path / "vehicle" / "afr_calibration.yaml"
    afr.parent.mkdir()
    base = profiles_disabled(_unscoped())
    config = base.model_copy(
        update={"daemon": base.daemon.model_copy(update={"afr_calibration": afr})}
    )
    absent = build_feature_frame_cache_key([session], config)
    afr.write_text(_scoped().daemon.afr_calibration.read_text())
    present = build_feature_frame_cache_key([session], config)
    afr.write_text(afr.read_text() + "\n# edited\n")
    edited = build_feature_frame_cache_key([session], config)
    assert len({absent, present, edited}) == 3


def test_the_feature_spec_records_the_calibration_by_content() -> None:
    """A manifest says which calibration its derived AFR channels were built
    with; ``sha256: None`` is the "without one" every pre-P02.11 frame had."""
    import hashlib

    from src.features.feature_pipeline import build_feature_spec

    features = featurize_wide_frame(
        _wide(), _scoped(), behavior_models=None, did_map=load_repo_did_map()
    ).features
    scoped = _scoped()
    spec = build_feature_spec(features, ["obd:RPM"], config=scoped)
    assert spec["afr_calibration"] == {
        "path": str(scoped.daemon.afr_calibration),
        "sha256": hashlib.sha256(scoped.daemon.afr_calibration.read_bytes()).hexdigest(),
    }
    old = profiles_disabled(_unscoped())
    assert build_feature_spec(features, ["obd:RPM"], config=old)["afr_calibration"] == {
        "path": str(old.daemon.afr_calibration),
        "sha256": None,
    }
    with pytest.raises(UnscopedConfigError):
        build_feature_spec(features, ["obd:RPM"], config=_unscoped())


# ---------------------------------------------------------------------------
# Every offline entry point hands featurization the scoped config
# ---------------------------------------------------------------------------


class _CapturedError(Exception):
    def __init__(self, config: CanaryConfig) -> None:
        super().__init__("captured")
        self.config = config


def _capture(*args: Any, **kwargs: Any) -> Any:
    for value in (*args, *kwargs.values()):
        if isinstance(value, CanaryConfig):
            raise _CapturedError(value)
    raise AssertionError("no config passed")


def test_train_anomaly_model_trains_on_the_scoped_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.train_anomaly_model as script

    monkeypatch.setattr(script, "train", _capture)
    session = tmp_path / "2026-09-20T152758"
    session.mkdir()
    with pytest.raises(_CapturedError) as captured:
        script.main([str(session), "--config", str(EXAMPLE_YAML), "--out", str(tmp_path / "if")])
    assert captured.value.config.scoped_profile == SLUG


def test_train_sequence_trains_on_the_scoped_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.train_sequence as script

    monkeypatch.setattr(script, "train", _capture)
    monkeypatch.setattr(script, "configure_torch_threads", lambda: None)
    session = tmp_path / "2026-09-20T152758"
    session.mkdir()
    backend = script.BACKBONES["lstm"]
    args = script.parse_args(
        backend, [str(session), "--config", str(EXAMPLE_YAML), "--out", str(tmp_path / "seq")]
    )
    with pytest.raises(_CapturedError) as captured:
        script._run(backend, args)
    assert captured.value.config.scoped_profile == SLUG


def test_retrain_runs_on_the_scoped_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.retrain as script
    from tests.configs import retrain_argv

    monkeypatch.setattr(script, "_start_run", _capture)
    with pytest.raises(_CapturedError) as captured:
        script.main(retrain_argv(tmp_path))
    assert captured.value.config.scoped_profile == SLUG


def test_the_evaluation_harness_scores_on_the_scoped_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.evaluate_detectors as harness

    seen: list[CanaryConfig] = []
    real = harness.PersistenceSpec.from_config

    def recording(config: CanaryConfig) -> Any:
        seen.append(config)
        return real(config)

    monkeypatch.setattr(harness.PersistenceSpec, "from_config", staticmethod(recording))
    (tmp_path / "sessions").mkdir()
    code = harness.main(
        [
            "--config",
            str(EXAMPLE_YAML),
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--if-model",
            str(tmp_path / "absent.joblib"),
            "--no-sequence",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--plan",
            "--out",
            str(tmp_path / "r.json"),
        ]
    )
    assert code == 0
    assert seen and all(config.scoped_profile == SLUG for config in seen)


def test_replay_featurizes_on_the_scoped_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.replay as script

    monkeypatch.setattr(script, "replay_sessions", _capture)
    with pytest.raises(_CapturedError) as captured:
        script.main([str(tmp_path), "--config", str(EXAMPLE_YAML)])
    assert captured.value.config.scoped_profile == SLUG
