"""W08.2: plain-image hashes in the flash history, and never-flashed reference images."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.vehicle.tune_revisions import (
    ReferenceImage,
    RegistryConflictError,
    TuneRevision,
    TuneRevisionRegistry,
    load_tune_revisions,
    merge_tune_registries,
)
from tests.rom_fixtures import ECU_READ_20261004_SHA256, PROFILE

A = "a" * 64
B = "b" * 64
T0 = datetime(2026, 8, 1, tzinfo=UTC)


def test_shipped_registry_names_the_decoded_images() -> None:
    registry = load_tune_revisions(PROFILE / "tune_revisions.yaml")
    assert registry is not None
    assert registry.match_image(ECU_READ_20261004_SHA256) == ("ZS44A-20260920", None)
    assert registry.match_image(
        "e01cc0cb45d5beb879f82931be3054c79b7be3955220b3d93045deaabd23f18e"
    ) == ("ZS44A-k106", None)
    assert registry.match_image(
        "8201208033e0926b4f5f5d758f0f4efdfa2cef39e069d0dffae2d950683bc203"
    ) == (None, "ZS44A-uprev-base")
    assert registry.match_image(A) == (None, None)
    # A reference image is not a flash: sessions are never assigned to it.
    assert all(r.revision != "ZS44A-uprev-base" for r in registry.revisions)


def test_one_image_cannot_be_two_calibrations() -> None:
    with pytest.raises(ValidationError, match="one image cannot be two calibrations"):
        TuneRevisionRegistry(
            revisions=[TuneRevision(revision="r1", flashed_at=T0, image_sha256=A)],
            reference_images=[ReferenceImage(name="base", image_sha256=A)],
        )
    with pytest.raises(ValidationError, match="unique"):
        TuneRevisionRegistry(
            reference_images=[
                ReferenceImage(name="base", image_sha256=A),
                ReferenceImage(name="base", image_sha256=B),
            ]
        )


def test_merge_fills_image_hash_from_the_tracked_file() -> None:
    """An operational copy written before image_sha256 existed is not a conflict."""
    path = Path("x.yaml")
    operational = TuneRevisionRegistry(revisions=[TuneRevision(revision="r1", flashed_at=T0)])
    tracked = TuneRevisionRegistry(
        revisions=[TuneRevision(revision="r1", flashed_at=T0, image_sha256=A)],
        reference_images=[ReferenceImage(name="base", image_sha256=B)],
    )
    merged = merge_tune_registries(operational, path, tracked, path)
    assert merged is not None
    assert merged.revisions[0].image_sha256 == A
    assert merged.match_image(B) == (None, "base")


def test_merge_refuses_two_different_image_hashes() -> None:
    path = Path("x.yaml")
    operational = TuneRevisionRegistry(
        revisions=[TuneRevision(revision="r1", flashed_at=T0, image_sha256=B)]
    )
    tracked = TuneRevisionRegistry(
        revisions=[TuneRevision(revision="r1", flashed_at=T0, image_sha256=A)]
    )
    with pytest.raises(RegistryConflictError, match="image_sha256"):
        merge_tune_registries(operational, path, tracked, path)


def test_append_flash_keeps_reference_images(tmp_path: Path) -> None:
    from src.control.calibration import append_flash
    from src.vehicle.profiles import active_registry
    from tests.configs import isolated_config

    config = isolated_config(tmp_path, daemon={"afr_calibration": PROFILE / "afr_calibration.yaml"})
    updated = append_flash(
        config, TuneRevision(revision="ZS44A-test-flash", flashed_at=datetime.now(UTC))
    )
    assert [r.name for r in updated.reference_images] == ["ZS44A-uprev-base"]
    registry = active_registry(config)
    assert registry is not None
    assert registry.match_image(ECU_READ_20261004_SHA256) == ("ZS44A-20260920", None)
    assert registry.revisions[-1].revision == "ZS44A-test-flash"
