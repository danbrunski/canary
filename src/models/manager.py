"""Hot-swap model loader (Task 5.16).

The IF and LSTM detectors are normally loaded once at daemon start. When a
retrain (5.17) writes a new artifact + manifest under the models directory,
``ModelManager`` notices, validates the new manifest against the running
model's schema via :mod:`src.models.manifest`, and atomically swaps the
in-memory reference. The next inference call uses the new model with no
process restart.

Two safety policies:

* **Manifest validation first.** A new manifest is rejected if it doesn't
  satisfy ``ModelManifestCommon`` *or* if its feature columns don't match
  the running model. Failed swaps leave the running model in place — the
  daemon keeps detecting; a noisy refit can't silently break inference.
* **Atomic swap.** ``current`` returns a snapshot of the in-flight model;
  ``poll`` rebuilds and replaces it under a lock. Inference threads
  reading ``current`` see either the old or new model, never a partially-
  constructed one. (Python attribute assignment is atomic enough; we use
  a lock around the *whole swap* — load + validate + assign — so two
  pollers can't race a half-loaded checkpoint into place.)

Polling rather than ``watchdog``-style fsnotify: simpler, no extra dep,
and the swap rate doesn't matter (refits are minute-to-hour scale).
``poll()`` is idempotent — feeds a watchdog/timer/explicit call equally
well; tests inject a fake clock and step through.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

from src.models.manifest import (
    FeatureSpecValidationError,
    ManifestValidationError,
    ModelManifestCommon,
    load_manifest_common,
    manifest_path_for,
    validate_model_feature_spec,
)

log = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class LoadedModel(Generic[T]):
    """A model bundle the manager is serving.

    ``artifact_path`` / ``manifest_path`` are tracked so ``poll`` can detect
    a newer manifest by ``mtime`` without re-validating an unchanged file
    on every tick.
    """

    artifact: T
    manifest: ModelManifestCommon
    artifact_path: Path
    manifest_path: Path
    manifest_mtime: float
    artifact_mtime: float = 0.0
    #: The artifact's own embedded manifest version, when the bundle carries
    #: one (both trainers embed a manifest inside the binary). ``None`` when
    #: the artifact doesn't expose it. When this differs from
    #: ``manifest.model_version`` the on-disk pair is inconsistent — a
    #: partial deploy (binaries travel by scp, manifests by git) — and
    #: *this* value is what inference actually serves.
    embedded_version: str | None = None

    @property
    def serving_version(self) -> str:
        """The version actually being served: the artifact's embedded version
        when known (that's the model doing the scoring), else the sidecar
        manifest's claim."""
        return self.embedded_version or self.manifest.model_version


@dataclass(frozen=True)
class SwapAttempt:
    """One ``poll()`` outcome — observable so the daemon's /status can surface it."""

    kind: str  # "swapped" | "no_change" | "rejected" | "missing"
    detail: str = ""
    new_version: str | None = None


ManifestSidecar = Callable[[Path], Path]
"""Function that, given an artifact path, returns the manifest path that pairs with it.

Both trainers use ``<artifact>.manifest.json``; this is configurable in case a
future trainer uses a different convention.
"""


def default_manifest_sidecar(artifact_path: Path) -> Path:
    return manifest_path_for(artifact_path)


def _embedded_artifact_version(artifact: Any) -> str | None:
    """The version string embedded in the artifact bundle itself, if any.

    Both trainers store a manifest inside the binary
    (``TrainedIsolationForest.manifest`` / ``TrainedSequenceModel.manifest``);
    duck-typed so a test's ``lambda p: object()`` loader simply yields
    ``None``."""
    version = getattr(getattr(artifact, "manifest", None), "model_version", None)
    return str(version) if version is not None else None


class ModelManager(Generic[T]):
    """Watches an artifact path, hot-swaps when a newer compatible manifest lands."""

    def __init__(
        self,
        artifact_path: Path,
        loader: Callable[[Path], T],
        *,
        initial: LoadedModel[T] | None = None,
        manifest_sidecar: ManifestSidecar = default_manifest_sidecar,
        allowed_model_types: tuple[str, ...] | None = None,
    ) -> None:
        """``loader`` builds the in-memory model from the artifact path.

        Pass it as ``load_isolation_forest_model`` / ``load_lstm_autoencoder_model``;
        decoupling lets the manager work for either detector kind, and lets
        tests pass a trivial ``lambda p: object()``.

        ``allowed_model_types`` restricts which ``model_type`` values are
        accepted on hot-swap — pass ``("isolation_forest",)`` to an IF
        manager so it can't accidentally accept an LSTM artifact named with
        the wrong path.
        """
        self._artifact_path = artifact_path
        self._loader = loader
        self._manifest_sidecar = manifest_sidecar
        self._allowed_model_types = allowed_model_types
        self._lock = threading.Lock()
        self._current = initial

    @property
    def current(self) -> LoadedModel[T] | None:
        """A snapshot of the model currently being served. ``None`` when nothing
        is loaded yet — caller is on the hook for that case."""
        return self._current

    def load_initial(self) -> SwapAttempt:
        """Load the artifact-path model at startup. Logs + skips if absent."""
        return self._maybe_load(_force=True)

    def poll(self) -> SwapAttempt:
        """Check the manifest; swap if a newer compatible one is on disk."""
        return self._maybe_load(_force=False)

    # ------------------------------------------------------------------ private

    def _maybe_load(self, *, _force: bool) -> SwapAttempt:
        manifest_path = self._manifest_sidecar(self._artifact_path)
        if not manifest_path.exists() or not self._artifact_path.exists():
            return SwapAttempt(
                kind="missing",
                detail=f"manifest or artifact missing at {self._artifact_path}",
            )
        try:
            mtime = manifest_path.stat().st_mtime
            artifact_mtime = self._artifact_path.stat().st_mtime
        except OSError as exc:
            return SwapAttempt(kind="missing", detail=f"stat failed: {exc}")

        with self._lock:
            if (
                not _force
                and self._current is not None
                and mtime <= self._current.manifest_mtime
                and artifact_mtime <= self._current.artifact_mtime
            ):
                return SwapAttempt(kind="no_change")

            try:
                manifest = load_manifest_common(
                    manifest_path, allowed_model_types=self._allowed_model_types
                )
                validate_model_feature_spec(
                    manifest.schema_,
                    context=f"manifest {manifest_path}",
                )
            except (ManifestValidationError, FeatureSpecValidationError) as exc:
                log.warning(
                    "rejecting new manifest", extra={"path": str(manifest_path), "error": str(exc)}
                )
                return SwapAttempt(kind="rejected", detail=str(exc))

            # Task 3.1: an in-sample-calibrated artifact underestimates
            # cross-session error and can flood alerts on unseen drives — not
            # a reason to refuse it (a smoke-test/dev-holdout artifact may be
            # deliberate), but the operator should know what's being served.
            if manifest.extras.get("calibration") == "in_sample":
                log.warning(
                    "serving in-sample-calibrated artifact",
                    extra={
                        "path": str(manifest_path),
                        "model_version": manifest.model_version,
                    },
                )

            try:
                artifact = self._loader(self._artifact_path)
            except Exception as exc:  # noqa: BLE001 — loader errors are caller's bag of varied types
                log.exception(
                    "loader failed",
                    extra={"path": str(self._artifact_path), "error": str(exc)},
                )
                return SwapAttempt(kind="rejected", detail=f"loader failed: {exc}")

            embedded_version = _embedded_artifact_version(artifact)
            if embedded_version is not None and embedded_version != manifest.model_version:
                # Seen live 2026-07-26: manifests landed on the Pi via git
                # while the gitignored binaries didn't, so /status claimed the
                # new version while inference served the old one. Serve anyway
                # (an old model beats no model), but say so loudly — and
                # ``serving_version`` reports the truth.
                log.error(
                    "artifact/manifest version mismatch — the manifest sidecar claims a "
                    "version the binary doesn't contain (partial deploy? binaries move "
                    "by scp, not git). Serving the artifact's own version.",
                    extra={
                        "artifact_path": str(self._artifact_path),
                        "manifest_version": manifest.model_version,
                        "artifact_version": embedded_version,
                    },
                )
                if self._current is not None:
                    # Sidecar and binary are deployed separately. Do not bind
                    # the running artifact to a manifest describing different
                    # weights; polling both mtimes ensures the later half of
                    # the pair is retried even when the sidecar is unchanged.
                    return SwapAttempt(
                        kind="rejected",
                        detail=(
                            "artifact/manifest version mismatch: "
                            f"artifact={embedded_version}, manifest={manifest.model_version}"
                        ),
                        new_version=manifest.model_version,
                    )
            loaded = LoadedModel(
                artifact=artifact,
                manifest=manifest,
                artifact_path=self._artifact_path,
                manifest_path=manifest_path,
                manifest_mtime=mtime,
                artifact_mtime=artifact_mtime,
                embedded_version=embedded_version,
            )
            prior_version = (
                self._current.manifest.model_version if self._current is not None else None
            )
            self._current = loaded
            # Task 6.7: version + training window + data-hash on every load, so
            # `tail_daemon_log` shows exactly what went live and when — no
            # corpus-vs-hash verification here (retention legitimately deletes
            # training sessions), just the facts a human would want in a log.
            log.info(
                "model swapped",
                extra={
                    "path": str(self._artifact_path),
                    "prior_version": prior_version,
                    "new_version": manifest.model_version,
                    "training_start_timestamp": manifest.training_start_timestamp,
                    "training_end_timestamp": manifest.training_end_timestamp,
                    "training_data_hash": manifest.training_data_hash,
                },
            )
            return SwapAttempt(kind="swapped", new_version=manifest.model_version)


def manager_for_artifact(
    artifact_path: Path,
    loader: Callable[[Path], Any],
    *,
    model_type: str | None = None,
    load: bool = True,
) -> ModelManager[Any]:
    """Convenience: build a ``ModelManager`` and load the initial artifact.

    ``load=False`` returns it empty (``current`` is ``None``); the first
    ``poll()`` then loads it, which is how the daemon keeps a cold artifact
    load off its start-up path.
    """
    allowed = (model_type,) if model_type is not None else None
    manager = ModelManager(artifact_path, loader=loader, allowed_model_types=allowed)
    if load:
        manager.load_initial()
    return manager


__all__ = [
    "LoadedModel",
    "ModelManager",
    "SwapAttempt",
    "default_manifest_sidecar",
    "manager_for_artifact",
]
