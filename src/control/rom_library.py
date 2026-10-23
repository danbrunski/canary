"""Per-vehicle ROM library: plain flash images, content-addressed (W08.3).

Images arrive as an upload (a plain image or an ``.OsirisROM``) or from the
ECU backup job (:mod:`src.control.rom_backup`). Each is stored once, under the
sha256 of its *plain* image, as ``<sha256>.bin`` beside ``<sha256>.json``;
adding the same image again keeps the one entry and records the new source.
The directory is ``daemon.rom_library_dir``, scoped per vehicle like the other
operational paths and outside the checkout: decoded ROM bytes never go into
Git.

Everything derived from the bytes — checksum verdict, calibration ID, map,
revision match — is computed when read, from the bytes and the vehicle's
current map and flash history, so an edit to either can never leave a stale
verdict on disk. A vehicle with no map still stores, lists, describes,
compares and downloads images (an ECU backup is how it gets one to map); only
the table views and the map's verdicts need the map, and say so. The metadata
file holds only what the bytes cannot say: where the image came from, when,
and the operator's label.

Read-only with respect to the ECU: the library stores and describes images.
Downloads are plain ``.bin``; there is no ``.OsirisROM`` export (W08 pre-made
decision).
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from src.analysis.rom_extent import ExtentSpec, read_extent
from src.analysis.rom_image import (
    ImageFormatError,
    checksum_verdict,
    compare_images,
    crc32_hex,
    describe,
    load_image,
    map_applies,
    read_calibration_id,
    revision_match,
    sha256_hex,
    tables_payload,
)
from src.analysis.rom_map import ROM_MAP_FILE, RomMap, load_rom_map
from src.atomic_write import atomic_write_text
from src.vehicle.tune_revisions import TuneRevisionRegistry

RomSource = Literal["upload_plain", "upload_osirisrom", "ecu_read"]

#: The upload cap (an ``.OsirisROM`` is a little over 1 MiB).
MAX_ROM_UPLOAD = 2 * 1024 * 1024
#: Longest label and filename kept.
MAX_LABEL_LENGTH = 120
MAX_FILENAME_LENGTH = 255
METADATA_VERSION = 1

_SHA = re.compile(r"^[0-9a-f]{64}$")


class RomLibraryError(ValueError):
    """A refused request; the message names why."""


class RomNotFoundError(KeyError):
    """No library entry with that sha256."""

    def __str__(self) -> str:
        return f"no ROM library entry {self.args[0]!r}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def valid_sha(sha: str) -> bool:
    return bool(_SHA.fullmatch(sha))


class RomLibrary:
    """One vehicle's images. Thread-safe; every write is atomic."""

    def __init__(
        self,
        root: Path,
        *,
        profile_dir: Path,
        registry: Callable[[], TuneRevisionRegistry | None],
        extent_spec: Callable[[], ExtentSpec | None] | None = None,
    ) -> None:
        self.root = Path(root)
        self.profile_dir = Path(profile_dir)
        self._registry = registry
        #: What an image says about its own size (the backup recipe's
        #: ``extent``); ``None`` or a ``None`` answer leaves ``extent`` null.
        self._extent_spec = extent_spec
        self._lock = threading.RLock()

    # -- definitions --------------------------------------------------------

    def rom_map(self) -> RomMap:
        """The vehicle's map. ``FileNotFoundError`` when it has none."""
        return load_rom_map(self.profile_dir)

    def rom_map_or_none(self) -> RomMap | None:
        """The vehicle's map, or ``None`` when it has none. An invalid map
        still raises ``ValueError``: broken is not the same as absent."""
        if not (self.profile_dir / ROM_MAP_FILE).exists():
            return None
        return load_rom_map(self.profile_dir)

    def registry(self) -> TuneRevisionRegistry | None:
        return self._registry()

    def extent(self, image: bytes) -> dict[str, Any] | None:
        """Whether ``image`` points past its own end, or ``None`` when the
        vehicle names no vector tables to read (:mod:`src.analysis.rom_extent`)."""
        spec = self._extent_spec() if self._extent_spec is not None else None
        return None if spec is None else read_extent(image, spec).to_dict()

    def revision_match(self, sha: str) -> dict[str, Any]:
        """Which recorded calibration ``sha`` is. Never raises.

        An unreadable or conflicting flash history is not "unrecognised" — the
        image was never compared — so it reads ``status: "unknown"`` with the
        reason. (A malformed operational registry raised straight through here
        and stranded a finished ECU read before it was stored.)
        """
        try:
            registry = self._registry()
        except Exception as exc:  # noqa: BLE001 - reported in the payload
            return {
                "revision": None,
                "status": "unknown",
                "reference": None,
                "reason": f"flash history unreadable: {type(exc).__name__}: {exc}",
            }
        return revision_match(sha, registry)

    # -- paths --------------------------------------------------------------

    def _bin(self, sha: str) -> Path:
        return self.root / f"{sha}.bin"

    def _meta(self, sha: str) -> Path:
        return self.root / f"{sha}.json"

    def _require(self, sha: str) -> None:
        if not valid_sha(sha) or not self._bin(sha).exists() or not self._meta(sha).exists():
            raise RomNotFoundError(sha)

    # -- writes -------------------------------------------------------------

    def add_upload(self, data: bytes, filename: str) -> tuple[dict[str, Any], bool]:
        """Store an uploaded file. Returns ``(entry, created)``.

        Refuses (``RomLibraryError``) an empty or oversized body, a missing
        filename, and any format :func:`load_image` refuses, by name.
        """
        name = (filename or "").strip()
        if not name:
            raise RomLibraryError("a filename is required")
        if not data:
            raise RomLibraryError("the upload is empty")
        if len(data) > MAX_ROM_UPLOAD:
            raise RomLibraryError(f"the upload exceeds the {MAX_ROM_UPLOAD:,}-byte (2 MiB) limit")
        try:
            loaded = load_image(data, self.rom_map_or_none())
        except ImageFormatError as exc:
            raise RomLibraryError(str(exc)) from exc
        source: RomSource = "upload_osirisrom" if loaded.format == "osirisrom" else "upload_plain"
        return self.add_image(
            loaded.image,
            source=source,
            filename=Path(name).name[:MAX_FILENAME_LENGTH],
            container_sha256=loaded.container_sha256,
        )

    def add_image(
        self,
        image: bytes,
        *,
        source: RomSource,
        filename: str | None = None,
        container_sha256: str | None = None,
        read_meta: dict[str, Any] | None = None,
        label: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Store a plain image (deduplicated by sha256) and record its source.

        With a map, the image must be the map's size; without one, any
        non-empty image (an ECU read is the recipe's range).
        """
        rom_map = self.rom_map_or_none()
        if not image:
            raise RomLibraryError("the image is empty")
        if rom_map is not None and len(image) != rom_map.image_size:
            raise RomLibraryError(
                f"image is {len(image):,} bytes; this vehicle's image is {rom_map.image_size:,}"
            )
        sha = sha256_hex(image)
        record: dict[str, Any] = {
            "source": source,
            "filename": filename,
            "added_at": _now(),
            "container_sha256": container_sha256,
            "read_meta": read_meta,
        }
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            meta_path = self._meta(sha)
            created = not meta_path.exists()
            meta: dict[str, Any]
            if created:
                tmp = self.root / f"{sha}.bin.{os.getpid()}.tmp"
                tmp.write_bytes(image)
                tmp.replace(self._bin(sha))
                meta = {
                    "schema_version": METADATA_VERSION,
                    "sha256": sha,
                    "size": len(image),
                    "crc32": crc32_hex(image),
                    "label": _clean_label(label),
                    "added_at": record["added_at"],
                    "sources": [record],
                }
            else:
                meta = self._read_meta(sha)
                meta["sources"].append(record)
                if label and not meta.get("label"):
                    meta["label"] = _clean_label(label)
            self._write_meta(sha, meta)
        return self.entry(sha), created

    def set_label(self, sha: str, label: str | None) -> dict[str, Any]:
        with self._lock:
            self._require(sha)
            meta = self._read_meta(sha)
            meta["label"] = _clean_label(label)
            self._write_meta(sha, meta)
        return self.entry(sha)

    def delete(self, sha: str) -> None:
        with self._lock:
            self._require(sha)
            self._meta(sha).unlink()
            self._bin(sha).unlink(missing_ok=True)

    # -- reads --------------------------------------------------------------

    def _read_meta(self, sha: str) -> dict[str, Any]:
        meta: dict[str, Any] = json.loads(self._meta(sha).read_text())
        return meta

    def _write_meta(self, sha: str, meta: dict[str, Any]) -> None:
        atomic_write_text(self._meta(sha), json.dumps(meta, indent=2, sort_keys=True) + "\n")

    def image(self, sha: str) -> bytes:
        self._require(sha)
        return self._bin(sha).read_bytes()

    def entry(self, sha: str) -> dict[str, Any]:
        """The list view of one entry: provenance plus what the bytes say."""
        self._require(sha)
        meta = self._read_meta(sha)
        return self._entry_from(meta, self._bin(sha).read_bytes())

    def _entry_from(self, meta: dict[str, Any], image: bytes) -> dict[str, Any]:
        rom_map = self.rom_map_or_none()
        sources: list[dict[str, Any]] = meta.get("sources") or []
        calibration_id = read_calibration_id(image, rom_map)
        reason = map_applies(calibration_id, rom_map)
        reads = [
            s["read_meta"] for s in sources if s.get("source") == "ecu_read" and s.get("read_meta")
        ]
        kinds: list[str] = []
        for record in sources:
            if record["source"] not in kinds:
                kinds.append(record["source"])
        return {
            "sha256": meta["sha256"],
            "crc32": meta["crc32"],
            "size": meta["size"],
            "container_sha256": next(
                (s["container_sha256"] for s in sources if s.get("container_sha256")), None
            ),
            "source": sources[0]["source"] if sources else None,
            "sources": kinds,
            "source_records": sources,
            "filename": next((s["filename"] for s in sources if s.get("filename")), None),
            "label": meta.get("label"),
            "added_at": meta["added_at"],
            "last_added_at": sources[-1]["added_at"] if sources else meta["added_at"],
            "calibration_id": calibration_id,
            "checksum": (
                checksum_verdict(image, rom_map.checksums)
                if rom_map is not None and reason is None
                else None
            ),
            "revision_match": self.revision_match(meta["sha256"]),
            "map_reason": reason,
            "extent": self.extent(image),
            "read_meta": reads[-1] if reads else None,
        }

    def entries(self) -> list[dict[str, Any]]:
        """Every entry, newest first (by the latest time it was added)."""
        if not self.root.exists():
            return []
        out = []
        for meta_path in self.root.glob("*.json"):
            sha = meta_path.stem
            if not valid_sha(sha) or not self._bin(sha).exists():
                continue
            out.append(self._entry_from(self._read_meta(sha), self._bin(sha).read_bytes()))
        return sorted(out, key=lambda e: e["last_added_at"], reverse=True)

    def describe(self, sha: str) -> dict[str, Any]:
        """The full W08.2 description, plus this entry's library metadata."""
        image = self.image(sha)
        entry = self.entry(sha)
        description = describe(
            image, self.rom_map_or_none(), None, container_sha256=entry["container_sha256"]
        )
        # The registry read that cannot raise (see revision_match).
        description["revision_match"] = entry["revision_match"]
        return {**description, "entry": entry}

    def tables(self, sha: str) -> dict[str, Any]:
        return tables_payload(self.image(sha), self.rom_map())

    def compare(self, a: str, b: str) -> dict[str, Any]:
        return compare_images(self.image(a), self.image(b), self.rom_map_or_none())


def _clean_label(label: str | None) -> str | None:
    if label is None:
        return None
    text = " ".join(label.split())
    if len(text) > MAX_LABEL_LENGTH:
        raise RomLibraryError(f"a label is at most {MAX_LABEL_LENGTH} characters")
    return text or None


__all__ = [
    "MAX_LABEL_LENGTH",
    "MAX_ROM_UPLOAD",
    "RomLibrary",
    "RomLibraryError",
    "RomNotFoundError",
    "RomSource",
    "valid_sha",
]
