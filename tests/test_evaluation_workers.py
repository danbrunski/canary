"""The scoring pool: its size, its memory ceiling and its spawned workers (P02.5, R15).

Moved from ``tests/test_evaluate_detectors_script.py`` when the pool left the
script for :mod:`src.models.evaluation_workers`. The patches below name that
module, because it is where the pool looks the names up; patching the script's
re-export would change nothing (``test_a_patch_on_the_script_does_not_reach_the_pool``).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def test_the_scoring_pool_is_capped_by_the_measured_worker_peak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--workers`` is a ceiling: memory (the measured per-worker peak, as if
    every worker peaked at once), cores at the pinned thread count, and the
    drive count each cap it; one worker never spawns a pool."""
    import src.models.evaluation_workers as harness

    peak = harness.SCORING_WORKER_PEAK_BYTES
    monkeypatch.setattr(harness.os, "cpu_count", lambda: 12)
    room = int(3 * peak / harness.SCORING_MEMORY_FRACTION) + 1
    monkeypatch.setattr(harness, "_available_memory_bytes", lambda: room)
    assert harness.scoring_workers(4, 18) == 3
    assert harness.scoring_workers(4, 2) == 2
    assert harness.scoring_workers(4, 18, threads=6) == 2
    assert harness.scoring_workers(1, 18) == 1
    monkeypatch.setattr(harness, "_available_memory_bytes", lambda: peak // 2)
    assert harness.scoring_workers(4, 18) == 1


def test_the_scoring_pool_respects_the_cgroup_and_stays_in_process_on_the_pi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.models.evaluation_workers as harness

    group = tmp_path / "cgroup" / "system.slice" / "canary.service"
    group.mkdir(parents=True)
    self_cgroup = tmp_path / "self_cgroup"
    self_cgroup.write_text("0::/system.slice/canary.service\n")
    (group / "memory.max").write_text(f"{6 * 1024**3}\n")
    (group / "memory.current").write_text(f"{1024**3}\n")
    headroom = harness._cgroup_memory_headroom(
        cgroup_root=tmp_path / "cgroup", self_cgroup=self_cgroup
    )
    assert headroom == 5 * 1024**3
    (group / "memory.max").write_text("max\n")
    assert (
        harness._cgroup_memory_headroom(cgroup_root=tmp_path / "cgroup", self_cgroup=self_cgroup)
        is None
    )

    peak = harness.SCORING_WORKER_PEAK_BYTES
    monkeypatch.setattr(harness.os, "cpu_count", lambda: 12)
    monkeypatch.setattr(harness, "on_the_pi", lambda: False)
    monkeypatch.setattr(harness, "_available_memory_bytes", lambda: 100 * peak)
    # The cgroup's headroom caps the pool below what MemAvailable would allow.
    room = int(2 * peak / harness.SCORING_MEMORY_FRACTION) + 1
    monkeypatch.setattr(harness, "_cgroup_memory_headroom", lambda: room)
    assert harness.scoring_workers(4, 18) == 2
    monkeypatch.setattr(harness, "_cgroup_memory_headroom", lambda: None)
    assert harness.scoring_workers(4, 18) == 4
    # On the Pi: in-process, whatever memory says, and no refusal.
    monkeypatch.setattr(harness, "on_the_pi", lambda: True)
    assert harness.scoring_workers(4, 18) == 1


def test_the_cgroup_headroom_is_the_tightest_level_up_to_the_root(tmp_path: Path) -> None:
    import src.models.evaluation_workers as harness

    root = tmp_path / "cgroup"
    leaf = root / "system.slice" / "canary.service"
    leaf.mkdir(parents=True)
    self_cgroup = tmp_path / "self_cgroup"
    self_cgroup.write_text("0::/system.slice/canary.service\n")
    (leaf / "memory.max").write_text("max\n")
    (leaf / "memory.current").write_text("100\n")
    (root / "system.slice" / "memory.max").write_text("1000\n")
    (root / "system.slice" / "memory.current").write_text("400\n")
    # The leaf is unlimited; its parent's ceiling binds it.
    assert harness._cgroup_memory_headroom(cgroup_root=root, self_cgroup=self_cgroup) == 600
    (leaf / "memory.max").write_text("300\n")
    assert harness._cgroup_memory_headroom(cgroup_root=root, self_cgroup=self_cgroup) == 200


def test_a_patch_on_the_script_does_not_reach_the_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """The script re-exports the pool's names (``_cgroup_memory_headroom``,
    ``SCORING_WORKER_PEAK_BYTES``) but ``scoring_workers`` looks them up in its
    own module. A test patching the script's copy changes nothing; the tests
    above patch the owner, and this one proves a patch there is what the pool
    reads."""
    import scripts.evaluate_detectors as script
    import src.models.evaluation_workers as owner

    peak = owner.SCORING_WORKER_PEAK_BYTES
    monkeypatch.setattr(owner.os, "cpu_count", lambda: 12)
    monkeypatch.setattr(owner, "on_the_pi", lambda: False)
    monkeypatch.setattr(owner, "_available_memory_bytes", lambda: 100 * peak)
    monkeypatch.setattr(owner, "_cgroup_memory_headroom", lambda: None)
    assert owner.scoring_workers(4, 18) == 4
    # A zero headroom and a per-worker peak larger than all memory would cap the
    # pool at one worker ...
    monkeypatch.setattr(script, "_cgroup_memory_headroom", lambda: 0)
    monkeypatch.setattr(script, "SCORING_WORKER_PEAK_BYTES", 1000 * peak)
    assert owner.scoring_workers(4, 18) == 4  # ... but only the owner's names are read
    monkeypatch.setattr(owner, "_cgroup_memory_headroom", lambda: 0)
    assert owner.scoring_workers(4, 18) == 1


def test_the_pool_is_spawned_not_forked_and_falls_back_in_process() -> None:
    """``score_drives`` uses the ``spawn`` start method (the parent has run
    torch, and a forked child of a live OpenMP pool can hang) and, when the pool
    breaks, scores in this process and returns every drive in task order."""
    import concurrent.futures as futures
    from unittest import mock

    import src.models.evaluation_workers as owner

    seen: dict[str, object] = {}

    class _Broken:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)

        def __enter__(self) -> _Broken:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def submit(self, *_a: object) -> object:
            raise futures.BrokenExecutor("a worker was killed")

    class _Scorer:
        def score_drive(self, task: Any) -> str:
            return f"scored {task.name}"

    tasks = [SimpleNamespace(name=f"t{i}", frames={"s": [0] * i}) for i in range(3)]
    with (
        mock.patch.object(owner, "ProcessPoolExecutor", _Broken),
        mock.patch.object(owner, "scoring_workers", lambda *_a, **_k: 2),
    ):
        results = owner.score_drives(
            tasks,  # type: ignore[arg-type]
            workers=2,
            threads=1,
            local=lambda: _Scorer(),  # type: ignore[arg-type,return-value]
            spec=lambda: "spec",  # type: ignore[arg-type,return-value]
        )
    assert results == ["scored t0", "scored t1", "scored t2"]
    assert seen["max_workers"] == 2
    assert seen["mp_context"].get_start_method() == "spawn"  # type: ignore[attr-defined]
    assert seen["initializer"] is owner._init_scoring_worker
    assert seen["initargs"] == ("spec",)
