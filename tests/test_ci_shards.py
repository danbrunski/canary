"""CI splits pytest across parallel jobs by hashing node ids (tests/conftest.py)."""

from __future__ import annotations

import pytest

from tests.conftest import in_shard, parse_shard


def test_every_test_lands_in_exactly_one_shard() -> None:
    ids = [f"tests/test_x.py::test_{n}" for n in range(500)]
    for count in (1, 3, 4):
        homes = [[i for i in range(count) if in_shard(node, i, count)] for node in ids]
        assert all(len(h) == 1 for h in homes)


def test_shards_are_roughly_balanced() -> None:
    ids = [f"tests/test_{n // 20}.py::test_{n}" for n in range(4000)]
    sizes = [sum(in_shard(node, i, 4) for node in ids) for i in range(4)]
    assert max(sizes) - min(sizes) < 0.1 * 1000


@pytest.mark.parametrize("bad", ["", "1", "4/4", "-1/4", "a/b", "1/0"])
def test_a_malformed_shard_is_refused(bad: str) -> None:
    with pytest.raises(pytest.UsageError):
        parse_shard(bad)


def test_a_valid_shard_parses() -> None:
    assert parse_shard("2/4") == (2, 4)
