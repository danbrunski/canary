"""Per-vehicle ROM library: plain flash images, content-addressed (W08.3).

Images arrive as an upload (a plain image or an ``.OsirisROM``), from the
ECU backup job (:mod:`src.control.rom_backup`), or as a ROM Editor export
(:func:`src.analysis.rom_editor.export_edits`: an edited, checksummed image
whose source record carries ``export_meta``, its provenance). Each is stored once, under the
sha256 of its *plain* image, as ``<sha256>.bin`` beside ``<sha256>.json``;
adding the same image again keeps the one entry and records the new source.
The directory is ``daemon.rom_library_dir``, scoped per vehicle like the other
operational paths and outside the checkout: decoded ROM bytes never go into
Git.

Everything derived from the bytes — checksum verdict, calibration ID, map,
revision match — is computed when read, from the bytes and the vehicle's
current map and flash history, so an edit to either can never leave a stale
verdict on disk.

Which map reads an image is ``map_lookup`` (``daemon.rom_map_lookup``,
:meth:`RomLibrary.map_for`). A Pi reads its own truck's ROMs, so
``active_profile`` reads every image with the scoped vehicle's map. A desk or a
companion opens anyone's ROM, so ``calibration_id`` finds the profile whose
``rom_map.yaml`` lists the image's calibration ID, among every
``<data>/<slug>/`` beside the active one; that profile's flash history (its
tracked ``tune_revisions.yaml``), simulator kit and typical values go with it.
Two maps naming one calibration is refused: which one reads the image would be
a guess. A vehicle with no map still stores, lists, describes,
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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from src.analysis.rom_extent import ExtentSpec, read_extent
from src.analysis.rom_image import (
    ImageFormatError,
    LoadedImage,
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
from src.vehicle.tune_revisions import TuneRevisionRegistry, load_tune_revisions

RomSource = Literal["upload_plain", "upload_osirisrom", "ecu_read", "rom_editor_export"]
#: How an image finds its map (``daemon.rom_map_lookup``; module docstring).
MapLookup = Literal["active_profile", "calibration_id"]

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


class RomMapLookupError(ValueError):
    """More than one profile's map names an image's calibration."""


@dataclass(frozen=True)
class MapMatch:
    """Which profile reads one image, with which map, and whether it describes it."""

    #: The profile whose map, flash history, simulator kit and typical values
    #: go with the image: the describing map's, else the active profile.
    profile_dir: Path
    #: The map that describes the image; when none does, the active profile's
    #: (or, without one, any profile's) so its calibration ID can still be read
    #: and ``reason`` says why it is unmapped. ``None`` only when no profile
    #: has a map.
    rom_map: RomMap | None
    calibration_id: str | None
    #: ``None`` when ``rom_map`` describes the image, else why not.
    reason: str | None


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
        map_lookup: MapLookup = "active_profile",
        profiles_root: Path | None = None,
    ) -> None:
        self.root = Path(root)
        #: The scoped (active) vehicle's tracked data directory.
        self.profile_dir = Path(profile_dir)
        #: Where the other profiles' ``<slug>/`` directories are (``data/``).
        self.profiles_root = Path(profiles_root) if profiles_root else self.profile_dir.parent
        self.map_lookup: MapLookup = map_lookup
        #: The active vehicle's flash history (tracked merged with operational).
        self._registry = registry
        #: What an image says about its own size (the backup recipe's
        #: ``extent``); ``None`` or a ``None`` answer leaves ``extent`` null.
        self._extent_spec = extent_spec
        self._lock = threading.RLock()

    # -- definitions --------------------------------------------------------

    def rom_map(self) -> RomMap:
        """The active vehicle's map (what its own ECU reads are checked
        against). ``FileNotFoundError`` when it has none. An image's map is
        :meth:`map_for`'s."""
        return load_rom_map(self.profile_dir)

    def rom_map_or_none(self) -> RomMap | None:
        """The active vehicle's map, or ``None`` when it has none. An invalid
        map still raises ``ValueError``: broken is not the same as absent."""
        if not (self.profile_dir / ROM_MAP_FILE).exists():
            return None
        return load_rom_map(self.profile_dir)

    def profile_maps(self) -> list[tuple[Path, RomMap]]:
        """The maps :meth:`map_for` chooses among, active profile first: only
        its own under ``active_profile``; under ``calibration_id`` also every
        other ``<profiles_root>/<slug>/rom_map.yaml``, by slug (``_``-prefixed
        directories skipped). Any invalid one raises ``ValueError``."""
        dirs = [self.profile_dir]
        if self.map_lookup == "calibration_id":
            active = self.profile_dir.resolve()
            dirs += [
                path.parent
                for path in sorted(self.profiles_root.glob(f"*/{ROM_MAP_FILE}"))
                if not path.parent.name.startswith("_") and path.parent.resolve() != active
            ]
        return [(d, load_rom_map(d)) for d in dirs if (d / ROM_MAP_FILE).exists()]

    def map_for(self, image: bytes, maps: Sequence[tuple[Path, RomMap]] | None = None) -> MapMatch:
        """The profile and map that read ``image`` (module docstring).

        ``maps`` is :meth:`profile_maps`, passed by a caller reading many
        images. ``RomMapLookupError`` when two maps name its calibration."""
        maps = self.profile_maps() if maps is None else maps
        found = []
        for profile, rom_map in maps:
            calibration_id = read_calibration_id(image, rom_map)
            if map_applies(calibration_id, rom_map) is None:
                found.append(MapMatch(profile, rom_map, calibration_id, None))
        if len(found) > 1:
            files = ", ".join(str(m.profile_dir / ROM_MAP_FILE) for m in found)
            raise RomMapLookupError(
                f"calibration {found[0].calibration_id} is in more than one map ({files}): "
                "a calibration has one map; remove it from all but one"
            )
        if found:
            return found[0]
        fallback = next((m for d, m in maps if d == self.profile_dir), None)
        if fallback is None and maps:
            fallback = maps[0][1]
        calibration_id = read_calibration_id(image, fallback)
        return MapMatch(
            self.profile_dir, fallback, calibration_id, map_applies(calibration_id, fallback)
        )

    def registry(self, profile_dir: Path | None = None) -> TuneRevisionRegistry | None:
        """The flash history of ``profile_dir`` (default the active vehicle):
        the active one's tracked and operational files merged; another
        profile's tracked ``tune_revisions.yaml``, the only one this machine has."""
        if profile_dir is None or Path(profile_dir) == self.profile_dir:
            return self._registry()
        return load_tune_revisions(Path(profile_dir) / "tune_revisions.yaml")

    def extent(self, image: bytes) -> dict[str, Any] | None:
        """Whether ``image`` points past its own end, or ``None`` when the
        vehicle names no vector tables to read (:mod:`src.analysis.rom_extent`)."""
        spec = self._extent_spec() if self._extent_spec is not None else None
        return None if spec is None else read_extent(image, spec).to_dict()

    def revision_match(self, sha: str, profile_dir: Path | None = None) -> dict[str, Any]:
        """Which recorded calibration ``sha`` is, in the flash history of
        ``profile_dir`` (default: the profile :meth:`map_for` picks for the
        image, when it is in the library). Never raises.

        An unreadable or conflicting flash history is not "unrecognised" — the
        image was never compared — so it reads ``status: "unknown"`` with the
        reason. (A malformed operational registry raised straight through here
        and stranded a finished ECU read before it was stored.) A map lookup
        that fails reads ``unknown`` the same way, naming the lookup.
        """
        if profile_dir is None:
            profile_dir = self.profile_dir
            if self.map_lookup == "calibration_id" and self._bin(sha).exists():
                try:
                    profile_dir = self.map_for(self._bin(sha).read_bytes()).profile_dir
                except ValueError as exc:
                    return {
                        "revision": None,
                        "status": "unknown",
                        "reference": None,
                        "reason": f"map lookup failed: {exc}",
                    }
        try:
            registry = self.registry(profile_dir)
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
        filename, and any format :func:`load_image` refuses, by name. Under
        ``calibration_id`` a file any profile's map accepts is an image; the
        refusal is the first map's.
        """
        name = (filename or "").strip()
        if not name:
            raise RomLibraryError("a filename is required")
        if not data:
            raise RomLibraryError("the upload is empty")
        if len(data) > MAX_ROM_UPLOAD:
            raise RomLibraryError(f"the upload exceeds the {MAX_ROM_UPLOAD:,}-byte (2 MiB) limit")
        loaded = self._load_upload(data)
        source: RomSource = "upload_osirisrom" if loaded.format == "osirisrom" else "upload_plain"
        return self.add_image(
            loaded.image,
            source=source,
            filename=Path(name).name[:MAX_FILENAME_LENGTH],
            container_sha256=loaded.container_sha256,
        )

    def _load_upload(self, data: bytes) -> LoadedImage:
        candidates: list[RomMap | None] = []
        if self.map_lookup == "calibration_id":
            candidates = [m for _, m in self.profile_maps()]
        candidates = candidates or [self.rom_map_or_none()]
        refusal: ImageFormatError | None = None
        for rom_map in candidates:
            try:
                return load_image(data, rom_map)
            except ImageFormatError as exc:
                refusal = refusal or exc
        assert refusal is not None
        raise RomLibraryError(str(refusal)) from refusal

    def add_image(
        self,
        image: bytes,
        *,
        source: RomSource,
        filename: str | None = None,
        container_sha256: str | None = None,
        read_meta: dict[str, Any] | None = None,
        export_meta: dict[str, Any] | None = None,
        label: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Store a plain image (deduplicated by sha256) and record its source.

        With a map (:meth:`map_for`'s), the image must be the map's size;
        without one, any non-empty image (an ECU read is the recipe's range). An export
        (``rom_editor_export``) must carry ``export_meta`` naming this image,
        and nothing else may.
        """
        if not image:
            raise RomLibraryError("the image is empty")
        rom_map = self.map_for(image).rom_map
        if rom_map is not None and len(image) != rom_map.image_size:
            raise RomLibraryError(
                f"image is {len(image):,} bytes; this vehicle's image is {rom_map.image_size:,}"
            )
        sha = sha256_hex(image)
        if (source == "rom_editor_export") != (export_meta is not None):
            raise RomLibraryError("export_meta comes with a rom_editor_export, and only then")
        if export_meta is not None and export_meta.get("image_sha256") != sha:
            raise RomLibraryError("export_meta names another image than the one stored")
        record: dict[str, Any] = {
            "source": source,
            "filename": filename,
            "added_at": _now(),
            "container_sha256": container_sha256,
            "read_meta": read_meta,
        }
        if export_meta is not None:
            record["export_meta"] = export_meta
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

    def _entry_from(
        self,
        meta: dict[str, Any],
        image: bytes,
        maps: Sequence[tuple[Path, RomMap]] | None = None,
    ) -> dict[str, Any]:
        match = self.map_for(image, maps)
        rom_map, calibration_id, reason = match.rom_map, match.calibration_id, match.reason
        sources: list[dict[str, Any]] = meta.get("sources") or []
        reads = [
            s["read_meta"] for s in sources if s.get("source") == "ecu_read" and s.get("read_meta")
        ]
        exports = [s["export_meta"] for s in sources if s.get("export_meta")]
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
            "revision_match": self.revision_match(meta["sha256"], match.profile_dir),
            "map_reason": reason,
            # The profile whose map describes the image (its data directory's
            # name), or null when none does.
            "map_profile": match.profile_dir.name if reason is None else None,
            "extent": self.extent(image),
            "read_meta": reads[-1] if reads else None,
            # The latest export that made these bytes (parent, spec, map, calibration).
            "export_meta": exports[-1] if exports else None,
        }

    def entries(self) -> list[dict[str, Any]]:
        """Every entry, newest first (by the latest time it was added)."""
        if not self.root.exists():
            return []
        out = []
        maps = self.profile_maps()
        for meta_path in self.root.glob("*.json"):
            sha = meta_path.stem
            if not valid_sha(sha) or not self._bin(sha).exists():
                continue
            out.append(self._entry_from(self._read_meta(sha), self._bin(sha).read_bytes(), maps))
        return sorted(out, key=lambda e: e["last_added_at"], reverse=True)

    def describe(self, sha: str) -> dict[str, Any]:
        """The full W08.2 description, plus this entry's library metadata."""
        image = self.image(sha)
        entry = self.entry(sha)
        description = describe(
            image, self.map_for(image).rom_map, None, container_sha256=entry["container_sha256"]
        )
        # The registry read that cannot raise (see revision_match).
        description["revision_match"] = entry["revision_match"]
        return {**description, "entry": entry}

    def tables(self, sha: str) -> dict[str, Any]:
        """The image's tables read with its map. ``FileNotFoundError`` when no
        profile has a map."""
        image = self.image(sha)
        rom_map = self.map_for(image).rom_map
        if rom_map is None:
            raise FileNotFoundError(self.profile_dir / ROM_MAP_FILE)
        return tables_payload(image, rom_map)

    def compare(self, a: str, b: str) -> dict[str, Any]:
        """Byte ranges between two images, labelled with ``a``'s map when it
        describes both (images of two calibrations come back unlabelled with
        the reason)."""
        image_a = self.image(a)
        return compare_images(image_a, self.image(b), self.map_for(image_a).rom_map)


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
    "MapLookup",
    "MapMatch",
    "RomLibrary",
    "RomLibraryError",
    "RomMapLookupError",
    "RomNotFoundError",
    "RomSource",
    "valid_sha",
]
