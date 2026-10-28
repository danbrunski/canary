"""The checksum writer against UpRev's own one-cell edits of the 2008 ROM.

Each case starts from the k106 file (``ZS44A-Tuned``), changes the one cell
UpRev changed, and writes the checksum words. UpRev saved the same edit, so
its file holds the right stored words: the written image must equal UpRev's
decoded image byte for byte. The words are also pinned here, so a file that
changed under the test cannot pass by agreeing with itself. The two cells sit
at opposite ends of their big-endian words (0xE075F is a word's low byte,
0xE025C its high byte), so a byte-order slip cannot pass both.

The files are the operator's, outside Git; each case skips when its file is
absent. Only hashes, offsets and checksum words are quoted, no ROM content.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.analysis.osiris import Container
from src.analysis.rom_image import checksum_values, sha256_hex
from src.analysis.rom_targets import Definition
from src.yaml_load import load_yaml
from tests.rom_fixtures import OPERATOR_ROMS, PROFILE

#: The k106 decoded image (tune_revisions.yaml ZS44A-k106 image_sha256) and the
#: words it stores.
K106_IMAGE_SHA256 = "e01cc0cb45d5beb879f82931be3054c79b7be3955220b3d93045deaabd23f18e"
K106_WORDS = (0xF438FCAA, 0x0C3BBAC0)

CASES = [
    pytest.param(
        "map1change",
        0xE075F,
        0x84,
        0x83,
        (0xF438FCA9, 0x0C3BBAC7),
        "3dc1f3d9c93615f84c86f463530e666183ff3c7f90f0bd27b4ab9556742a76c5",
        id="fuel-comp-cell-map1change",
    ),
    pytest.param(
        "r20260920",
        0xE025C,
        0xA2,
        0xA3,
        (0xF538FCAA, 0x0D3BBAC0),
        "a46baa248f16db8d5ed9854913e614dd7d77b7579bb6c36bddb792741531be50",
        id="fuel-targets-cell-20260920",
    ),
]


def _decoded(path: Path) -> bytes:
    if not path.exists():
        pytest.skip(f"operator ROM {path.name} not here")
    return Container.parse(path.read_bytes()).decoded()


@pytest.fixture(scope="module")
def definition() -> Definition:
    return Definition.model_validate(load_yaml((PROFILE / "rom_targets.yaml").read_text()))


@pytest.mark.parametrize(("edited", "offset", "old", "new", "words", "image_sha256"), CASES)
def test_the_writer_reproduces_uprevs_stored_words_for_a_one_cell_edit(
    definition: Definition,
    edited: str,
    offset: int,
    old: int,
    new: int,
    words: tuple[int, int],
    image_sha256: str,
) -> None:
    checksums = definition.checksums
    base = _decoded(OPERATOR_ROMS["k106"])
    uprev = _decoded(OPERATOR_ROMS[edited])
    assert sha256_hex(base) == K106_IMAGE_SHA256
    assert sha256_hex(uprev) == image_sha256
    assert checksums.stored(base) == K106_WORDS
    assert checksums.stored(uprev) == words

    stale = bytearray(base)
    assert stale[offset] == old
    stale[offset] = new
    with pytest.raises(ValueError, match="checksum mismatch"):
        checksums.verify(bytes(stale))

    written = checksums.write(bytes(stale))
    assert (written.total, written.xor) == words
    assert written.stored == K106_WORDS
    assert checksum_values(written.rom, checksums) == words
    assert written.rom == uprev
