"""``-n auto`` caps the torch tier's workers (tests/conftest.py, T01.18)."""

from __future__ import annotations

import pytest

from tests.conftest import selects_ml_training_tier


@pytest.mark.parametrize("markexpr", ["ml_training", "ml_training or slow", "slow or ml_training"])
def test_the_torch_tier_on_its_own_is_capped(markexpr: str) -> None:
    assert selects_ml_training_tier(markexpr)


@pytest.mark.parametrize(
    "markexpr",
    [
        # The default addopts, the push gate's inner loop and the other tiers.
        "not integration and not ml_training",
        "not integration and not ml_training and not slow",
        "slow",
        "integration",
        # Everything: the torch tests are a sliver among light ones.
        "",
        "not integration",
    ],
)
def test_other_selections_keep_every_core(markexpr: str) -> None:
    assert not selects_ml_training_tier(markexpr)
