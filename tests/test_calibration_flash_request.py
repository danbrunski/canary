"""POST /control/calibration carries the plain image hash into the registry (W08.2)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.dashboard.routes.calibration import FlashRequest
from src.vehicle.tune_revisions import TuneRevision

IMAGE = "5587f7e1e6dbd0fcb9ffe0c857cc0cafebbe11bf306636c0f3265b903254d253"
CONTAINER = "33d5d7a5e33ccddc4747821984973716e8902bd0d5c5f26f79c109cfbe26b858"


def test_a_recorded_flash_keeps_its_image_hash() -> None:
    body = FlashRequest(
        revision="ZS44A-20261004",
        flashed_at=datetime(2026, 10, 4, 23, 2, 33, tzinfo=UTC),
        rom_sha256=CONTAINER,
        image_sha256=IMAGE,
    )
    revision = TuneRevision.model_validate(body.model_dump())
    assert revision.image_sha256 == IMAGE
    assert revision.rom_sha256 == CONTAINER


def test_the_image_hash_is_optional() -> None:
    body = FlashRequest(revision="r", flashed_at=datetime(2026, 10, 4, tzinfo=UTC))
    assert TuneRevision.model_validate(body.model_dump()).image_sha256 is None


def test_a_malformed_image_hash_is_refused() -> None:
    with pytest.raises(ValidationError):
        FlashRequest(revision="r", flashed_at=datetime(2026, 10, 4, tzinfo=UTC), image_sha256="ABC")
