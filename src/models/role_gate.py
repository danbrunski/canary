"""A06.5 — the daemon's side of the alert budget: effective detector roles.

``models.roles`` says what a detector is *configured* to be. Since the
operator's 2026-09-23 decision, an ``alert`` role takes effect only for an
artifact whose sha256 has a passing alert-budget verdict beside it
(``<artifact>.alert-budget.json``, written by ``scripts/retrain.py`` after
``src.models.alert_budget`` held it to ≤ 1 alert per engine-hour, upper
bound, on held-out drives). Anything else — no verdict, a verdict for other
bytes, a failed verdict (a publish override included: it is not a passing
verdict), an unreadable one — serves as ``evidence``, and
``/status`` (``health.detector_roles``) names why.

This closes the path the publish-time refusal alone left open: flipping
``models.roles`` to ``alert`` in the Pi's config, with an artifact nobody
measured. The 2026-09-10 sequence detector alerted ~117 times per
engine-hour under exactly that kind of unexamined ``alert``.

The gate is a read-only ``Mapping[source, role]`` of *effective* roles, so
the emitter consults it on every event, and the live detectors stamp their
fusion sources from it: ``FusionScorer`` never promotes an ``evidence``
source, so demoting here is enough. Resolution is cached per loaded artifact
and recomputed at the first lookup after a hot-swap — a swap to an artifact
without a verdict demotes at the swap. ``refresh`` (called by the model
poller off the event loop) also picks up a verdict that lands after its
binary.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from src.config import DetectorRole
from src.models.alert_budget import (
    ALERT_BUDGET_VERDICT_SCHEMA,
    CADENCE_BOUND,
    CADENCE_INCIDENT,
    file_sha256,
    recorded_cadence,
    verdict_path_for,
)

#: The event source whose verdicts depend on the sequence cadence (A06.6).
SEQUENCE_SOURCE = "lstm_autoencoder"

log = logging.getLogger(__name__)

ALERT = "alert"
EVIDENCE = "evidence"


@dataclass(frozen=True)
class RoleResolution:
    """One detector's configured and effective role, and why."""

    source: str
    configured: str
    effective: str
    reason: str
    artifact_path: str | None = None
    artifact_sha256: str | None = None
    model_version: str | None = None
    verdict_path: str | None = None
    #: The numbers the verdict rests on (bound, detectors, drives, date).
    verdict: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "effective": self.effective,
            "reason": self.reason,
            "artifact_path": self.artifact_path,
            "artifact_sha256": self.artifact_sha256,
            "model_version": self.model_version,
            "verdict_path": self.verdict_path,
            "verdict": None if self.verdict is None else dict(self.verdict),
        }


def _verdict_summary(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "sha256": record.get("sha256"),
        "evaluated_at": record.get("evaluated_at"),
        "accepted": record.get("accepted"),
        "passed": record.get("passed"),
        "budget_per_engine_hour": record.get("budget_per_engine_hour"),
        "bound": record.get("bound"),
        "detectors": record.get("detectors"),
        "violations": record.get("violations"),
        "override_reason": record.get("override_reason"),
        "evaluation_drives": record.get("evaluation_drives"),
        "sequence_forward_every_n_ticks": recorded_cadence(record),
    }


def _short(digest: str | None) -> str:
    return "?" if not digest else digest[:12]


def resolve_role(
    source: str,
    configured: str,
    *,
    artifact_path: Path | None,
    model_version: str | None = None,
    forward_every_n_ticks: int = 1,
) -> RoleResolution:
    """The effective role of ``source`` serving the artifact at ``artifact_path``.

    Only a demotion is possible: ``evidence`` stays ``evidence``; ``alert``
    needs a passing verdict naming this artifact's sha256. A06.6: for the
    sequence source, and for any verdict that graded a sequence-bound detector
    (the fused verdict), it must also have been measured at the served
    ``forward_every_n_ticks``.
    """
    if configured != ALERT:
        return RoleResolution(source, configured, EVIDENCE, "configured evidence (models.roles)")
    if artifact_path is None:
        return RoleResolution(
            source, configured, EVIDENCE, "alert role refused: no artifact loaded"
        )
    base: dict[str, Any] = {"artifact_path": str(artifact_path), "model_version": model_version}
    try:
        digest = file_sha256(artifact_path)
    except OSError as exc:
        return RoleResolution(
            source,
            configured,
            EVIDENCE,
            f"alert role refused: cannot hash the artifact: {exc}",
            **base,
        )
    base["artifact_sha256"] = digest
    verdict_file = verdict_path_for(artifact_path)
    base["verdict_path"] = str(verdict_file)
    if not verdict_file.exists():
        return RoleResolution(
            source,
            configured,
            EVIDENCE,
            f"alert role refused: no passing alert-budget verdict for {_short(digest)}",
            **base,
        )
    try:
        record = json.loads(verdict_file.read_text())
        if not isinstance(record, Mapping):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as exc:
        return RoleResolution(
            source,
            configured,
            EVIDENCE,
            f"alert role refused: unreadable alert-budget verdict {verdict_file.name}: {exc}",
            **base,
        )
    summary = _verdict_summary(record)
    if record.get("schema_version") != ALERT_BUDGET_VERDICT_SCHEMA:
        reason = (
            f"alert role refused: alert-budget verdict schema "
            f"{record.get('schema_version')!r}, expected {ALERT_BUDGET_VERDICT_SCHEMA}"
        )
        return RoleResolution(source, configured, EVIDENCE, reason, verdict=summary, **base)
    if record.get("sha256") != digest:
        reason = (
            f"alert role refused: no passing alert-budget verdict for {_short(digest)} "
            f"(the verdict beside it is for {_short(str(record.get('sha256')))})"
        )
        return RoleResolution(source, configured, EVIDENCE, reason, verdict=summary, **base)
    if record.get("violations"):
        violations = "; ".join(str(v) for v in record.get("violations") or ())
        override = record.get("override_reason")
        reason = (
            f"alert role refused: alert-budget verdict for {_short(digest)} failed: {violations}"
            + (
                f" (published under --accept-alert-budget {override!r}; an override is not "
                "a passing verdict)"
                if override
                else ""
            )
        )
        return RoleResolution(source, configured, EVIDENCE, reason, verdict=summary, **base)
    if not record.get("passed"):
        reason = f"alert role refused: alert-budget verdict for {_short(digest)} is not passing"
        return RoleResolution(source, configured, EVIDENCE, reason, verdict=summary, **base)
    graded = {
        str(d.get("detector")) for d in record.get("detectors") or () if isinstance(d, Mapping)
    }
    scored_at = recorded_cadence(record)
    if (source == SEQUENCE_SOURCE or graded & CADENCE_BOUND) and scored_at != int(
        forward_every_n_ticks
    ):
        reason = (
            f"alert role refused: the alert-budget verdict for {_short(digest)} measured the "
            f"sequence detector every {scored_at} tick(s), but it serves every "
            f"{forward_every_n_ticks} (models.sequence_forward_every_n_ticks); "
            f"{CADENCE_INCIDENT}"
        )
        return RoleResolution(source, configured, EVIDENCE, reason, verdict=summary, **base)
    reason = (
        f"alert: passing alert-budget verdict for {_short(digest)} ({record.get('evaluated_at')})"
    )
    return RoleResolution(source, configured, ALERT, reason, verdict=summary, **base)


LoadedLookup = Callable[[str], Any]
"""``source -> LoadedModel | None`` — whatever the model managers serve now."""


class DetectorRoleGate(Mapping[str, DetectorRole]):
    """Effective roles keyed by event/fusion source name.

    Sources not in ``configured`` (behavior, DTC, trend, fused, ...) are not
    in the mapping, so a ``.get(source, "alert")`` caller keeps treating them
    as alerts, as before.
    """

    def __init__(
        self,
        configured: Mapping[str, str],
        *,
        loaded_for: LoadedLookup | None = None,
        on_change: Callable[[dict[str, Any]], None] | None = None,
        forward_every_n_ticks: int = 1,
    ) -> None:
        self._configured = dict(configured)
        #: A06.6 — the served sequence cadence a verdict must have been measured at.
        self._forward_every_n_ticks = int(forward_every_n_ticks)
        self._loaded_for = loaded_for
        self._on_change = on_change
        self._lock = threading.Lock()
        #: source -> (the LoadedModel it was resolved for, verdict mtime, resolution)
        self._cache: dict[str, tuple[Any, float | None, RoleResolution]] = {}

    def bind(
        self,
        loaded_for: LoadedLookup,
        *,
        on_change: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Attach the model lookup (and the ``/status`` publisher) once the
        components exist, and resolve every source."""
        self._loaded_for = loaded_for
        if on_change is not None:
            self._on_change = on_change
        self.refresh()
        self._publish()

    def _loaded(self, source: str) -> Any:
        return None if self._loaded_for is None else self._loaded_for(source)

    def resolution(self, source: str, *, recheck_verdict: bool = False) -> RoleResolution:
        configured = self._configured[source]
        loaded = self._loaded(source) if configured == ALERT else None
        artifact = getattr(loaded, "artifact_path", None) if loaded is not None else None
        verdict_mtime = (
            _mtime(verdict_path_for(Path(artifact))) if recheck_verdict and artifact else None
        )
        with self._lock:
            cached = self._cache.get(source)
            if (
                cached is not None
                and cached[0] is loaded
                and (not recheck_verdict or cached[1] == verdict_mtime)
            ):
                return cached[2]
        result = resolve_role(
            source,
            configured,
            artifact_path=None if artifact is None else Path(artifact),
            model_version=getattr(loaded, "serving_version", None),
            forward_every_n_ticks=self._forward_every_n_ticks,
        )
        if artifact is not None and verdict_mtime is None:
            verdict_mtime = _mtime(verdict_path_for(Path(artifact)))
        with self._lock:
            previous = self._cache.get(source)
            self._cache[source] = (loaded, verdict_mtime, result)
        if previous is None or previous[2] != result:
            if result.configured == ALERT and result.effective != ALERT:
                log.warning("detector %s serves as evidence: %s", source, result.reason)
            self._publish()
        return result

    def refresh(self) -> None:
        """Re-resolve every source, re-reading verdict files that changed.
        Called off the event loop by the model poller (it hashes artifacts)."""
        for source in self._configured:
            self.resolution(source, recheck_verdict=True)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            cached = {source: entry[2] for source, entry in self._cache.items()}
        return {
            source: (
                cached[source].to_dict()
                if source in cached
                else {"configured": configured, "effective": None, "reason": "not resolved yet"}
            )
            for source, configured in self._configured.items()
        }

    def _publish(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change(self.snapshot())
            except Exception:  # noqa: BLE001 — /status reporting never breaks scoring
                log.exception("publishing detector roles failed")

    # Mapping protocol: effective roles.
    def __getitem__(self, source: str) -> DetectorRole:
        if source not in self._configured:
            raise KeyError(source)
        return cast(DetectorRole, self.resolution(source).effective)

    def __iter__(self) -> Iterator[str]:
        return iter(self._configured)

    def __len__(self) -> int:
        return len(self._configured)


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


__all__ = ["DetectorRoleGate", "RoleResolution", "resolve_role"]
