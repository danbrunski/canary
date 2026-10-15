"""Arrived-sample streams for the evaluation's raw-stream detectors (A02.5).

The plausibility detector reads samples as they arrived, not the featurized
rows. Each ``(detector, session)`` stream is built once into the cache as a
parquet file, and :class:`CachedStreams` reads it back on each access without
holding it: holding every session's stream at once was most of the harness's
memory. Extracted from ``scripts/evaluate_detectors.py`` (R15.3).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import CanaryConfig
from src.features.feature_cache import build_feature_frame_cache_key
from src.models.evaluation_detectors import RawStreamDetector


class CachedStreams(Mapping[tuple[str, str], Any]):
    """Arrived-sample streams by ``(detector, session)``, read from their cache
    file on each access and never held.

    Holding every session's stream at once was most of the harness's memory:
    RSS rose from 2.3 GB to 6.0 GB while the A02.11 streams loaded (2.4 GB of
    samples plus what the allocator kept). Before P02.4 the longest session's
    stream was read twice per drive (clean scoring, then the injection sweep,
    which reused it for every injection); :class:`DriveScorer` now reads each
    once per drive, and not at all when every score it feeds is cached.
    """

    def __init__(self, paths: Mapping[tuple[str, str], Path]) -> None:
        self._paths = dict(paths)

    @property
    def paths(self) -> dict[tuple[str, str], Path]:
        return dict(self._paths)

    def name_of(self, key: tuple[str, str]) -> str:
        """The stream's cache-file name: its session key and catalog
        fingerprint, so a score keyed on it follows an edited limit."""
        return self._paths[key].name

    def __getitem__(self, key: tuple[str, str]) -> Any:
        from src.plausibility.offline import SessionStream

        frame = pd.read_parquet(self._paths[key])
        modes = frame.pop("mode").to_numpy(dtype=object)
        modes[modes == "None"] = None
        return SessionStream(key[1], frame.reset_index(drop=True), modes)

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(self._paths)

    def __len__(self) -> int:
        return len(self._paths)


def load_raw_streams(
    detectors: Sequence[RawStreamDetector],
    sessions: Sequence[Path],
    config: CanaryConfig,
    *,
    cache_dir: Path,
) -> CachedStreams:
    """Each arrived-sample detector's stream per session, cached as parquet.

    The key is the session's feature-frame cache key (session file stats,
    bindings, DBC, DID map, preprocessing) plus the detector's own catalog
    fingerprint, so an edited limit or binding cannot be served a stale stream.
    Streams are built one at a time into the cache and read back on use
    (:class:`CachedStreams`), so only the one in use is in memory.
    """
    paths: dict[tuple[str, str], Path] = {}
    for detector in detectors:
        # Every resolved limit, policy and source, hashed stably across
        # processes (a repr hash followed PYTHONHASHSEED and never hit).
        catalog = getattr(detector, "catalog", None)
        fingerprint = (
            catalog.fingerprint()[:12]
            if catalog is not None
            else hashlib.sha256(b"no catalog").hexdigest()[:12]
        )
        for session in sessions:
            key = build_feature_frame_cache_key([session], config)
            path = cache_dir / detector.names[0] / f"{key}-{fingerprint}.parquet"
            if not path.exists():
                stream = detector.load(session)
                path.parent.mkdir(parents=True, exist_ok=True)
                stream.samples.assign(mode=stream.modes.astype(str)).to_parquet(path)
                del stream
            paths[(detector.names[0], session.name)] = path
    return CachedStreams(paths)
