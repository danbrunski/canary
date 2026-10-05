"""Where frozen references, candidate checkpoints and promotion decisions live (E03.6).

One directory per vehicle under ``state_dir`` (the brain job passes
``brain_dir(config) / "candidates"``), one subdirectory per residual::

    <root>/<residual>/references/<fingerprint>.json   FrozenReference + its policy
    <root>/<residual>/checkpoints/<fingerprint>.json  CandidateCheckpoint + its spec
    <root>/<residual>/active.json                     which reference monitors now
    <root>/<residual>/decisions.jsonl                 every evaluation decision

What the layout makes impossible rather than documented:

* **Nothing is deleted.** A reference file is written once under its own
  fingerprint and never removed, so a promotion's predecessor stays on disk
  (recoverable with :meth:`CandidateStore.restore`). Checkpoints of failed
  candidates stay too; a rejected decision is a line, not a deletion.
* **Promotion is one atomic rename.** The promoted reference is written first
  (under its fingerprint, not yet active); then ``active.json`` is replaced by
  ``atomic_write_text``. A crash between the two leaves the old reference
  active and the new one inert on disk; a reader never sees half a promotion.
* **Compare-and-swap.** :meth:`CandidateStore.activate` takes the reference it
  expects to replace. A second promotion of the same candidate finds itself
  already active and changes nothing; a promotion decided against a reference
  that is no longer active is refused, so a stale decision cannot overwrite a
  newer one.
* **Self-hashed on read.** References and checkpoints validate their own
  fingerprints when loaded, and must sit under the file name of that
  fingerprint; an edited file is refused, never served.
* **A decision recorded twice is one line.** Decisions are keyed by their
  content, so replaying the same evidence does not grow the log.

JSON + stdlib + the kernels' pydantic models (layer ``models``).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.atomic_write import atomic_write_text
from src.models.adaptive_candidate import CandidateCheckpoint, CandidateSpec
from src.models.change_detection import ChangePolicy, FrozenReference

__all__ = [
    "CANDIDATES_DIRNAME",
    "CandidateStore",
    "CandidateStoreError",
    "StaleActivationError",
]

#: The store's directory name under the vehicle's brain directory.
CANDIDATES_DIRNAME = "candidates"

_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,95}$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")


class CandidateStoreError(ValueError):
    """A store operation refused (unknown residual, edited file, bad name)."""


class StaleActivationError(CandidateStoreError):
    """A promotion decided against a reference that is no longer active."""


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=1, sort_keys=True, default=str) + "\n"


def _key(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


class CandidateStore:
    """One vehicle's references, candidate checkpoints and promotion decisions."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # -- paths ----------------------------------------------------------------

    def _dir(self, residual: str) -> Path:
        if not _NAME.fullmatch(residual):
            raise CandidateStoreError(f"not a residual name: {residual!r}")
        return self.root / residual

    @staticmethod
    def _fp(fingerprint: str) -> str:
        if not _FINGERPRINT.fullmatch(fingerprint):
            raise CandidateStoreError(f"not a fingerprint: {fingerprint!r}")
        return fingerprint

    @contextmanager
    def _locked(self, residual: str) -> Iterator[None]:
        directory = self._dir(residual)
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / ".lock").open("a") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield

    # -- references -----------------------------------------------------------

    def _write_reference(
        self, residual: str, reference: FrozenReference, policy: ChangePolicy
    ) -> None:
        if reference.residual != residual:
            raise CandidateStoreError(f"reference models {reference.residual!r}, not {residual!r}")
        if policy.sha256() != reference.policy_sha256:
            raise CandidateStoreError("the policy is not the one the reference was frozen under")
        path = self._dir(residual) / "references" / f"{reference.fingerprint}.json"
        if path.exists():
            return  # written once, under its own fingerprint
        atomic_write_text(
            path,
            _dump(
                {
                    "reference": reference.model_dump(mode="json"),
                    "policy": policy.model_dump(mode="json", by_alias=True),
                }
            ),
            mkdir=True,
        )

    def stage_reference(
        self, residual: str, reference: FrozenReference, policy: ChangePolicy
    ) -> None:
        """Write a reference under its fingerprint without activating it."""
        with self._locked(residual):
            self._write_reference(residual, reference, policy)

    def reference(self, residual: str, fingerprint: str) -> FrozenReference:
        """Any stored reference (active, previous or never activated)."""
        return self._read_reference(residual, fingerprint)[0]

    def policy(self, residual: str, fingerprint: str) -> ChangePolicy:
        return self._read_reference(residual, fingerprint)[1]

    def _read_reference(
        self, residual: str, fingerprint: str
    ) -> tuple[FrozenReference, ChangePolicy]:
        path = self._dir(residual) / "references" / f"{self._fp(fingerprint)}.json"
        if not path.is_file():
            raise CandidateStoreError(f"no stored reference {fingerprint[:16]} for {residual}")
        data = json.loads(path.read_text())
        reference = FrozenReference.model_validate(data["reference"])  # self-hash checked
        policy = ChangePolicy.model_validate(data["policy"])
        if reference.fingerprint != fingerprint or policy.sha256() != reference.policy_sha256:
            raise CandidateStoreError(f"{path.name} does not hold the reference it is named for")
        return reference, policy

    def references(self, residual: str) -> list[str]:
        directory = self._dir(residual) / "references"
        return sorted(p.stem for p in directory.glob("*.json")) if directory.is_dir() else []

    def install_reference(
        self,
        residual: str,
        reference: FrozenReference,
        policy: ChangePolicy,
        *,
        now: datetime | None = None,
    ) -> None:
        """Make a held-out fit the first active reference for ``residual``.

        Refused when another reference is already active: replacing a working
        reference is a promotion (:meth:`activate`), which needs a decision.
        """
        if reference.lineage != "held_out_fit":
            raise CandidateStoreError("only a held-out fit is installed; a candidate is promoted")
        with self._locked(residual):
            current = self._active_payload(residual)
            if current is not None:
                if current["reference"] == reference.fingerprint:
                    return
                raise CandidateStoreError(
                    f"{residual} already has an active reference "
                    f"({str(current['reference'])[:16]}); replacing it is a promotion"
                )
            self._write_reference(residual, reference, policy)
            self._write_active(
                residual,
                {
                    "reference": reference.fingerprint,
                    "history": [],
                    "activated_at": (now or datetime.now(UTC)).isoformat(),
                    "decision": None,
                },
            )

    # -- the active pointer ---------------------------------------------------

    def _active_payload(self, residual: str) -> dict[str, Any] | None:
        path = self._dir(residual) / "active.json"
        if not path.is_file():
            return None
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or not _FINGERPRINT.fullmatch(str(data.get("reference"))):
            raise CandidateStoreError(f"{path} is not an active-reference pointer")
        return data

    def _write_active(self, residual: str, payload: dict[str, Any]) -> None:
        atomic_write_text(self._dir(residual) / "active.json", _dump(payload), mkdir=True)

    def active_fingerprint(self, residual: str) -> str | None:
        payload = self._active_payload(residual)
        return None if payload is None else str(payload["reference"])

    def active(self, residual: str) -> FrozenReference | None:
        fingerprint = self.active_fingerprint(residual)
        return None if fingerprint is None else self.reference(residual, fingerprint)

    def history(self, residual: str) -> list[dict[str, Any]]:
        """Earlier active references, oldest first, each with when and why it left."""
        payload = self._active_payload(residual)
        return [] if payload is None else list(payload.get("history", []))

    def activate(
        self,
        residual: str,
        reference: FrozenReference,
        policy: ChangePolicy,
        *,
        expected_active: str,
        decision: str,
        now: datetime | None = None,
    ) -> bool:
        """Atomically make ``reference`` the active one; ``True`` if this call did.

        ``expected_active`` is the reference the decision was made against.
        Already active: nothing changes (``False``). Another reference active:
        :class:`StaleActivationError`. The previous reference stays on disk
        and in ``history``.
        """
        with self._locked(residual):
            current = self._active_payload(residual)
            if current is None:
                raise CandidateStoreError(f"{residual} has no active reference to replace")
            if current["reference"] == reference.fingerprint:
                return False
            if current["reference"] != expected_active:
                raise StaleActivationError(
                    f"decision {decision} was made against {expected_active[:16]}, but "
                    f"{str(current['reference'])[:16]} is active now; evaluate again"
                )
            self._write_reference(residual, reference, policy)  # first: inert until the swap
            stamp = (now or datetime.now(UTC)).isoformat()
            history = [
                *current.get("history", []),
                {
                    "reference": current["reference"],
                    "activated_at": current.get("activated_at"),
                    "replaced_at": stamp,
                    "replaced_by": reference.fingerprint,
                    "decision": decision,
                },
            ]
            self._write_active(
                residual,
                {
                    "reference": reference.fingerprint,
                    "history": history,
                    "activated_at": stamp,
                    "decision": decision,
                },
            )
            return True

    def restore(
        self, residual: str, fingerprint: str, *, reason: str, now: datetime | None = None
    ) -> None:
        """Make an earlier reference active again (its file never left)."""
        reference, policy = self._read_reference(residual, fingerprint)
        current = self.active_fingerprint(residual)
        if current is None:
            raise CandidateStoreError(f"{residual} has no active reference")
        self.activate(
            residual,
            reference,
            policy,
            expected_active=current,
            decision=f"restore: {reason}",
            now=now,
        )

    # -- checkpoints ----------------------------------------------------------

    def save_checkpoint(
        self, residual: str, checkpoint: CandidateCheckpoint, spec: CandidateSpec
    ) -> str:
        """Persist a checkpoint (idempotent); returns its fingerprint."""
        if spec.residual != residual or checkpoint.spec_sha256 != spec.sha256():
            raise CandidateStoreError("the checkpoint was not written under this spec")
        path = self._dir(residual) / "checkpoints" / f"{checkpoint.fingerprint}.json"
        if not path.exists():
            atomic_write_text(
                path,
                _dump(
                    {
                        "checkpoint": checkpoint.model_dump(mode="json"),
                        "spec": spec.model_dump(mode="json", by_alias=True),
                    }
                ),
                mkdir=True,
            )
        return checkpoint.fingerprint

    def checkpoint(
        self, residual: str, fingerprint: str
    ) -> tuple[CandidateCheckpoint, CandidateSpec]:
        path = self._dir(residual) / "checkpoints" / f"{self._fp(fingerprint)}.json"
        if not path.is_file():
            raise CandidateStoreError(f"no stored checkpoint {fingerprint[:16]} for {residual}")
        data = json.loads(path.read_text())
        checkpoint = CandidateCheckpoint.model_validate(data["checkpoint"])  # self-hash checked
        spec = CandidateSpec.model_validate(data["spec"])
        if checkpoint.fingerprint != fingerprint or checkpoint.spec_sha256 != spec.sha256():
            raise CandidateStoreError(f"{path.name} does not hold the checkpoint it is named for")
        return checkpoint, spec

    def checkpoints(self, residual: str) -> list[str]:
        directory = self._dir(residual) / "checkpoints"
        return sorted(p.stem for p in directory.glob("*.json")) if directory.is_dir() else []

    # -- decisions ------------------------------------------------------------

    def record_decision(self, residual: str, decision: dict[str, Any]) -> str:
        """Append ``decision`` unless an identical one is already recorded;
        returns its key (the same key for the same content)."""
        key = _key(decision)
        with self._locked(residual):
            if any(entry["key"] == key for entry in self.decisions(residual)):
                return key
            path = self._dir(residual) / "decisions.jsonl"
            torn = (
                path.is_file() and path.stat().st_size > 0 and not path.read_bytes().endswith(b"\n")
            )
            with path.open("a") as handle:
                if torn:
                    handle.write("\n")  # terminate a torn tail before appending
                handle.write(json.dumps({"key": key, **decision}, sort_keys=True, default=str))
                handle.write("\n")
        return key

    def decisions(self, residual: str) -> list[dict[str, Any]]:
        path = self._dir(residual) / "decisions.jsonl"
        if not path.is_file():
            return []
        out: list[dict[str, Any]] = []
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn tail; the next append starts a fresh line
            if isinstance(entry, dict) and "key" in entry:
                out.append(entry)
        return out
