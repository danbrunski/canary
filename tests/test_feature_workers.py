"""R14: the parallel per-session build's decisions, pinned where the output cannot show them.

``test_build_feature_frame_parallel_matches_serial`` shows the pool and the
serial path agree; it cannot show *which* ran. These tests drive
``resample_sessions`` with the pool replaced by a recording fake, so the
threshold, the spawn context, the initializer arguments, the order of results,
the skip-and-name behavior and the fall back to serial after a dead worker are
each asserted. The decode itself is replaced by a stub: nothing here reads a
session.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterator, Sequence
from concurrent.futures import BrokenExecutor
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from src.features import feature_workers
from src.features.session_resampling import BuildDecodeInputs
from src.preprocessing.resampler import ResampleConfig

CONFIG = ResampleConfig()
INPUTS = BuildDecodeInputs(bindings=None, behavior_models=None, dbc_database="dbc", did_map="dids")


def _frame(name: str) -> pd.DataFrame:
    return pd.DataFrame({"session": [name, name], "obd:RPM": [800.0, 810.0]})


def _sessions(*names: str) -> list[Path]:
    return [Path("sessions") / name for name in names]


class FakePool:
    """Records how it was built; ``map`` yields the worker entry point's results."""

    instances: list[FakePool] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.mapped: list[Path] = []
        FakePool.instances.append(self)

    def __enter__(self) -> FakePool:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def map(
        self,
        fn: Callable[[Path], tuple[str, pd.DataFrame | None, str | None]],
        dirs: Sequence[Path],
    ) -> Iterator[tuple[str, pd.DataFrame | None, str | None]]:
        self.mapped = list(dirs)
        # The initializer runs once per worker process before any task.
        self.kwargs["initializer"](*self.kwargs["initargs"])
        yield from (fn(d) for d in dirs)


@pytest.fixture(autouse=True)
def fake_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    FakePool.instances = []
    monkeypatch.setattr(feature_workers, "ProcessPoolExecutor", FakePool)
    monkeypatch.setattr(feature_workers, "_FEATURE_WORKER_STATE", {})


def _decode(monkeypatch: pytest.MonkeyPatch, failing: Collection[str] = ()) -> list[str]:
    seen: list[str] = []

    def decode(session_dir: Path, *args: Any) -> pd.DataFrame:
        seen.append(session_dir.name)
        if session_dir.name in failing:
            raise RuntimeError(f"unreadable {session_dir.name}")
        return _frame(session_dir.name)

    monkeypatch.setattr(feature_workers, "decode_session_to_resampled", decode)
    return seen


def test_below_the_threshold_the_pool_is_never_built(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _decode(monkeypatch)
    frame = feature_workers.resample_sessions(_sessions("a", "b"), INPUTS, CONFIG, max_workers=4)
    assert FakePool.instances == []
    assert seen == ["a", "b"]
    assert list(frame["session"].unique()) == ["a", "b"]


def test_at_the_threshold_the_pool_is_spawned_with_the_catalogs_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _decode(monkeypatch)
    frame = feature_workers.resample_sessions(
        _sessions("a", "b", "c"), INPUTS, CONFIG, max_workers=2
    )
    (pool,) = FakePool.instances
    assert pool.kwargs["max_workers"] == 2
    assert pool.kwargs["initializer"] is feature_workers._init_feature_worker
    assert pool.kwargs["initargs"] == (None, "dbc", "dids", CONFIG)
    assert pool.kwargs["mp_context"].get_start_method() == "spawn"
    # ``map`` preserves order, so the frame equals the serial one.
    assert pool.mapped == _sessions("a", "b", "c")
    assert list(frame["session"].unique()) == ["a", "b", "c"]
    assert list(frame.index) == list(range(len(frame)))


def test_one_worker_is_serial_whatever_the_session_count(monkeypatch: pytest.MonkeyPatch) -> None:
    _decode(monkeypatch)
    feature_workers.resample_sessions(_sessions("a", "b", "c", "d"), INPUTS, CONFIG, max_workers=1)
    assert FakePool.instances == []


def test_an_unreadable_session_is_skipped_and_named_in_both_paths(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _decode(monkeypatch, failing={"b"})
    with caplog.at_level("WARNING", logger="src.features.feature_workers"):
        serial = feature_workers.resample_sessions(
            _sessions("a", "b", "c"), INPUTS, CONFIG, max_workers=1
        )
        pooled = feature_workers.resample_sessions(
            _sessions("a", "b", "c"), INPUTS, CONFIG, max_workers=2
        )
    pd.testing.assert_frame_equal(serial, pooled)
    assert list(serial["session"].unique()) == ["a", "c"]
    messages = [r.getMessage() for r in caplog.records]
    assert sum("skipping unreadable session" in m for m in messages) == 2
    assert sum("2/3 sessions; 1 skipped: b" in m for m in messages) == 2


def test_no_readable_session_raises_and_names_every_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    _decode(monkeypatch, failing={"a", "b", "c"})
    with pytest.raises(ValueError, match=r"no readable sessions among the 3 given.*a, b, c"):
        feature_workers.resample_sessions(_sessions("a", "b", "c"), INPUTS, CONFIG, max_workers=2)


def test_a_dead_worker_discards_the_pool_results_and_rebuilds_serially(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    seen = _decode(monkeypatch, failing={"b"})

    class Dying(FakePool):
        def map(self, fn: Any, dirs: Sequence[Path]) -> Iterator[Any]:
            self.mapped = list(dirs)
            self.kwargs["initializer"](*self.kwargs["initargs"])
            yield fn(dirs[0])  # a good result, then the worker is killed
            yield fn(dirs[1])  # b: reported as a skip, not an exception
            raise BrokenExecutor("worker killed")

    monkeypatch.setattr(feature_workers, "ProcessPoolExecutor", Dying)
    with caplog.at_level("WARNING", logger="src.features.feature_workers"):
        frame = feature_workers.resample_sessions(
            _sessions("a", "b", "c"), INPUTS, CONFIG, max_workers=2
        )
    messages = [r.getMessage() for r in caplog.records]
    assert any("falling back to serial" in m for m in messages)
    # Serial from scratch: every session decoded again, the pool's frames and
    # its skip list dropped (b is skipped once, by the serial pass).
    assert seen == ["a", "b", "a", "b", "c"]
    assert list(frame["session"]) == ["a", "a", "c", "c"], "no frame twice, none from the pool"
    assert sum("1 skipped: b" in m for m in messages) == 1


def test_the_worker_entry_point_reports_an_error_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _decode(monkeypatch, failing={"bad"})
    feature_workers._init_feature_worker("bindings", "dbc", "dids", CONFIG)
    state = feature_workers._FEATURE_WORKER_STATE
    assert state == {"bindings": "bindings", "dbc": "dbc", "dids": "dids", "resample": CONFIG}

    name, frame, error = feature_workers._worker_process_session(Path("x/good"))
    assert (name, error) == ("good", None)
    pd.testing.assert_frame_equal(frame, _frame("good"))  # type: ignore[arg-type]

    name, frame, error = feature_workers._worker_process_session(Path("x/bad"))
    assert (name, frame, error) == ("bad", None, "RuntimeError: unreadable bad")


def test_the_stale_row_filter_runs_on_the_concatenated_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def decode(session_dir: Path, *args: Any) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "session": [session_dir.name] * 3,
                "obd:RPM": [800.0, 800.0, 810.0],
                "obd:RPM__gap": [False, True, False],
            }
        )

    monkeypatch.setattr(feature_workers, "decode_session_to_resampled", decode)
    frame = feature_workers.resample_sessions(_sessions("a"), INPUTS, CONFIG, max_workers=1)
    assert len(frame) == 2, "the all-stale row went"
    assert list(frame.index) == [0, 1]
