"""The ECU backup's post-processing: integrity, then the library (R29.13).

Nothing here touches the bus. :func:`store_image` takes the bytes a finished
read produced and the counters' summary, checks them
(:func:`integrity`), and adds the image to the :class:`RomLibrary` with its
``read_meta``. An unverified image is kept and marked, never hidden.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Literal

from src.analysis.rom_extent import ExtentReading, read_extent
from src.analysis.rom_image import (
    checksum_verdict,
    map_applies,
    read_calibration_id,
    sha256_hex,
)
from src.analysis.rom_map import RomMap
from src.control.rom_backup_recipe import BackupRecipe
from src.control.rom_library import RomLibrary

log = logging.getLogger("src.control.rom_backup")

BackupStateName = Literal["idle", "refused", "running", "done", "failed", "cancelled"]


def store_image(
    image: bytes,
    recipe: BackupRecipe,
    rom_map: RomMap | None,
    summary: dict[str, Any],
    reread_mismatches: list[str],
    *,
    library: RomLibrary,
    recipe_source: str,
    ecu_calibration_id: Callable[[], str | None],
    update: Callable[..., None],
) -> tuple[BackupStateName, str | None]:
    """Add the image to the library, then report its integrity.

    Unverified is kept, never hidden: ``failed`` when a check disagreed,
    ``done`` with the reason when a check had nothing to check against.
    Nothing that can raise runs before
    :meth:`RomLibrary.add_image`: the integrity checks are wrapped (a
    failure there is itself a reason the image is unverified) and the
    revision match cannot raise (a broken flash history reads ``unknown``
    with its reason), so a bad registry or a callback error can no longer
    lose a ten-minute read. ``update`` publishes the outcome to the job state.
    """
    unchecked: list[str] = []
    try:
        integrity_payload, failures, unchecked = integrity(
            image, recipe, rom_map, reread_mismatches, ecu_calibration_id
        )
    except Exception as exc:  # noqa: BLE001 - recorded as the reason, never raised
        log.exception("ROM backup: integrity check failed")
        integrity_payload = None
        failures = [f"the integrity check failed: {type(exc).__name__}: {exc}"]
    problems = failures + unchecked
    verified = not problems
    sha = sha256_hex(image)
    match = library.revision_match(sha)
    read_meta = {
        **summary,
        "blocks": len(recipe.read.blocks()),
        "block_size": recipe.read.block_size,
        "range": [f"0x{recipe.read.start:06X}", f"0x{recipe.read.end:06X}"],
        "request_id": f"0x{recipe.request_id:03X}",
        "response_id": f"0x{recipe.response_id:03X}",
        "recipe": recipe_source,
        "verified": verified,
        "unverified_reason": None if verified else "; ".join(problems),
        "integrity": integrity_payload,
        "revision_match": match,
    }
    try:
        library.add_image(image, source="ecu_read", read_meta=read_meta)
    except Exception as exc:  # noqa: BLE001 - named in the state; nothing was stored
        log.exception("could not store the ECU read")
        update(stored=False, sha256=None, verified=verified, integrity=integrity_payload)
        return "failed", (
            f"the read finished but the image could not be stored "
            f"({type(exc).__name__}: {exc}); nothing was kept"
        )
    update(
        stored=True,
        sha256=sha,
        verified=verified,
        integrity=integrity_payload,
        revision_match=match,
    )
    if verified:
        return "done", None
    return ("failed" if failures else "done"), "unverified: " + "; ".join(problems)


def integrity(
    image: bytes,
    recipe: BackupRecipe,
    rom_map: RomMap | None,
    reread_mismatches: list[str],
    ecu_calibration_id: Callable[[], str | None],
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Checksums, calibration ID against the ECM's, and the re-read.

    Returns the payload, the *failures* (a check ran and disagreed) and
    the *unchecked* (a check had nothing to check against).
    """
    failures: list[str] = []
    unchecked: list[str] = []
    calibration_id = read_calibration_id(image, rom_map)
    no_map = map_applies(calibration_id, rom_map)
    checksum = None
    if rom_map is None or no_map is not None:
        unchecked.append(f"{no_map}: checksums not checked")
    else:
        checksum = checksum_verdict(image, rom_map.checksums)
        if not checksum["ok"]:
            failures.append(checksum["reason"] or "checksum mismatch")
    extent = read_extent(image, recipe.extent) if recipe.extent is not None else None
    if extent is not None and extent.verdict == "larger":
        failures.append(extent.summary())
    elif rom_map is None:
        unchecked.append(size_caveat(recipe, extent))
    # Blank is as good as unreported: an empty string is found anywhere.
    ecu_id = (ecu_calibration_id() or "").strip() or None
    offset: int | None = None
    if rom_map is not None:
        # The map says where the ID lives, whether or not it describes this
        # calibration: a different ID there is the image disagreeing with
        # the ECM, which is exactly what makes the map not apply.
        offset = rom_map.calibration_id.offset
    elif ecu_id is not None and ecu_id.isascii() and ecu_id.isprintable():
        # Nothing says where the ID lives: look for the ECM's own as ASCII.
        found = image.find(ecu_id.encode("ascii"))
        if found >= 0:
            offset, calibration_id = found, ecu_id
    if ecu_id is None:
        unchecked.append(
            "the ECM has not reported its calibration ID (/status calibration), so the "
            f"image's {calibration_id or 'calibration ID'} could not be checked against it"
        )
    elif rom_map is not None and ecu_id != calibration_id:
        failures.append(f"image calibration ID {calibration_id} != ECM-reported {ecu_id}")
    elif rom_map is None and offset is None:
        unchecked.append(
            f"the ECM reports calibration ID {ecu_id}, and the image does not hold it as ASCII"
        )
    if reread_mismatches:
        failures.append(
            f"{len(reread_mismatches)} re-read block(s) differ: {', '.join(reread_mismatches)}"
        )
    payload = {
        "checksum": checksum,
        "calibration_id": {
            "image": calibration_id,
            "ecu": ecu_id,
            "ok": ecu_id is not None and ecu_id == calibration_id,
            "offset": None if offset is None else f"0x{offset:06X}",
        },
        "reread": {
            "blocks": min(recipe.verify.reread_blocks, len(recipe.read.blocks())),
            "mismatches": reread_mismatches,
        },
        "extent": None if extent is None else extent.to_dict(),
    }
    return payload, failures, unchecked


def size_caveat(recipe: BackupRecipe, extent: ExtentReading | None) -> str:
    """Without a map, what (if anything) checked the image's size."""
    size = f"{recipe.read.size:,}"
    if extent is not None and extent.verdict == "no_sign":
        return (
            f"with no map, only the image's own pointers check its size: {extent.summary()}; "
            f"a sign, not proof, that the recipe's {size} bytes are the whole flash"
        )
    caveat = f"with no map, nothing confirms the recipe's {size} bytes are the whole flash"
    return caveat if extent is None else f"{caveat} ({extent.summary()})"


__all__ = ["BackupStateName", "integrity", "size_caveat", "store_image"]
