"""S01.2 / S01.3 — nothing on the path to recording imports what recording
doesn't need.

A cold boot on the Pi reads every imported module off the SD card before the
event loop starts the collectors. On 2026-09-22 the ``py-spy`` trace showed
torch (620 MB on disk) arriving at t ~ 33-40 s, through ``src.daemon.runtime``
-> ``replay_control`` -> ``alerts.emitter`` -> ``src.models`` (whose package
``__init__`` imported everything) -> ``models.inference`` ->
``lstm_autoencoder``, whose class bases import ``torch.nn``. Nothing needed it
to record.

Each check runs in a fresh interpreter: the test process has long since
imported all of these, so an in-process ``sys.modules`` check would pass or
fail for reasons unrelated to the import graph under test.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: What the daemon runs on the main thread before the collector tasks do: the
#: CLI entry point, the daemon facade, the collectors, and ``build_components``
#: exactly as ``run_daemon`` calls it (models deferred to the poller) — with
#: both detectors enabled, so their managers are built. ``{config}`` is a
#: config file under the test's tmp dir.
_RECORDING_PATH_IMPORTS = (
    "import src.main\n"
    "import src.daemon\n"
    "import src.daemon.runtime\n"
    "from src.collection.collectors import default_collectors\n"
    "from pathlib import Path\n"
    "from src.config import CanaryConfig\n"
    "from src.daemon import build_components\n"
    "config = CanaryConfig.from_yaml(Path({config!r}))\n"
    "components = build_components(config, load_models=False)\n"
    "assert set(components.model_managers) == {{'isolation_forest', 'lstm_autoencoder'}}, "
    "components.model_managers\n"
    "default_collectors(config)\n"
)


def _modules_loaded_by(imports: str) -> set[str]:
    result = subprocess.run(
        [sys.executable, "-c", f"import sys\n{imports}print('\\n'.join(sorted(sys.modules)))\n"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return set(result.stdout.split())


@pytest.fixture(scope="module")
def recording_path_modules(tmp_path_factory: pytest.TempPathFactory) -> set[str]:
    # The property under test is "torch is installed and still not imported".
    # Without torch, build_components skips the sequence detector ("enabled but
    # torch unavailable"), so the both-managers premise cannot hold and the
    # import check proves nothing. CI does not install the `ml` extra; this
    # errored all 18 dependent tests there on 2026-09-23.
    pytest.importorskip("torch")
    from tests.test_daemon import _config_yaml

    tmp = tmp_path_factory.mktemp("cold_start")
    text = _config_yaml(tmp)
    # Both detectors on: their managers are what used to import the IF loader.
    text = text.replace(
        "  isolation_forest:\n    enabled: false", "  isolation_forest:\n    enabled: true"
    )
    text = text.replace(
        "  lstm_autoencoder:\n    enabled: false", "  lstm_autoencoder:\n    enabled: true"
    )
    assert text.count("enabled: true") >= 2
    config_path = tmp / "canary.yaml"
    config_path.write_text(text)
    return _modules_loaded_by(_RECORDING_PATH_IMPORTS.format(config=str(config_path)))


def test_recording_path_does_not_import_torch(recording_path_modules: set[str]) -> None:
    """S01.2: torch arrives with the first sequence-model load, in a thread."""
    assert "torch" not in recording_path_modules


def test_models_package_imports_nothing_eagerly() -> None:
    """S01.2: any ``src.models.*`` import executes the package ``__init__``."""
    loaded = _modules_loaded_by("import src.models\n")
    assert not {name for name in loaded if name.startswith("src.models.")}
    assert "torch" not in loaded


def test_models_package_lazy_reexports_still_resolve() -> None:
    import src.models as models

    assert models.SEQUENCE_BACKENDS is not None
    assert models.SequenceInferenceEngine.__name__ == "SequenceInferenceEngine"
    with pytest.raises(AttributeError):
        _ = models.not_a_model_export  # type: ignore[attr-defined]


def test_sequence_inference_module_does_not_import_torch() -> None:
    """S01.2: ``lstm_autoencoder`` is imported by the loader, never by
    ``models.inference`` at module level."""
    loaded = _modules_loaded_by("import src.models.inference\n")
    assert "torch" not in loaded
    assert "src.models.lstm_autoencoder" not in loaded


#: S01.3 — modules moved off the path to recording, each with where it now
#: loads. Laptop, warm, min of 7 (`import src.daemon` after `import src.main`):
#: 2,746 ms and 2,198 modules before, 161 ms and 1,063 modules after.
_MOVED_OFF_THE_RECORDING_PATH = {
    # The dashboard API tree: built in serve_in_background's thread.
    "src.dashboard.api": "server._build_app",
    "fastapi": "server._build_app",
    "uvicorn": "server._build_app",
    # The pandas feature pipeline, decode layer and scoring worker: in the
    # thread that builds the live source.
    "src.daemon.inference": "runtime.build_live_feature_source",
    "src.preprocessing.live": "runtime.build_live_feature_source",
    "src.preprocessing.feature_pipeline": "runtime.build_live_feature_source",
    # Isolation Forest (and through it the FSM limits and pdfplumber): the
    # emitter wanted five column names from it.
    "src.models.isolation_forest": "the model poller's thread",
    "pdfplumber": "the model poller's thread",
    "sklearn": "the model poller's thread",
    # The trip-end digest and its analysis modules: imported in a thread
    # when a trip ends.
    "src.control.trip_end": "runtime.handle_trip_end_off_loop",
    "src.analysis.digest": "runtime.handle_trip_end_off_loop",
    # The preprocessing package used to import every submodule eagerly.
    "src.preprocessing.anomaly_injection": "first use",
    # ...and the IF loader, via build_components' model managers, pulled in
    # normalization with the rest of the feature pipeline.
    "src.preprocessing.normalization": "components._load_isolation_forest",
    "pdfminer": "the model poller's thread",
    # The ELM327 transport's library: only that transport imports it now.
    "obd": "collectors (elm327 branch) / src.collection.obd_collector",
    "pint": "collectors (elm327 branch) / src.collection.obd_collector",
    # ntfy pushes: the client is built in a thread at the first push.
    "httpx": "notifier._new_client",
}


@pytest.mark.parametrize("module", sorted(_MOVED_OFF_THE_RECORDING_PATH))
def test_recording_path_does_not_import(module: str, recording_path_modules: set[str]) -> None:
    """S01.3: each module moved off the path stays off it."""
    assert module not in recording_path_modules, (
        f"{module} is imported before recording again; it belongs in "
        f"{_MOVED_OFF_THE_RECORDING_PATH[module]}"
    )


def test_preprocessing_package_lazy_reexports_still_resolve() -> None:
    import src.preprocessing as preprocessing
    from src.preprocessing import MODE_COLUMN, feature_pipeline

    assert MODE_COLUMN == preprocessing.MODE_COLUMN
    assert feature_pipeline.__name__ == "src.preprocessing.feature_pipeline"
    with pytest.raises(AttributeError):
        _ = preprocessing.not_a_preprocessing_export  # type: ignore[attr-defined]


def test_daemon_package_lazy_reexports_still_resolve() -> None:
    import src.daemon as daemon
    from src.daemon import inference

    assert daemon.build_live_feature_source is inference.build_live_feature_source
    assert daemon._score_and_emit is inference._score_and_emit
    with pytest.raises(AttributeError):
        _ = daemon.not_a_daemon_export  # type: ignore[attr-defined]
