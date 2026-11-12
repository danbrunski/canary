"""``load_yaml_model``: a YAML file into a model, once, with the file named in
the error (R24.3), and the catalogs that share its cache (R26.3)."""

from __future__ import annotations

import hashlib
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest
from pydantic import BaseModel, ConfigDict

from src.analysis.channels import raw_read_filters, read_channels
from src.decode.bindings import load_bindings
from src.decode.dids import load_did_map
from src.paths import BINDINGS_YAML, DIDS_YAML
from src.yaml_load import YamlModelError, clear_yaml_model_cache, load_yaml_model


class _Doc(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    name: str = "x"


class _OtherDoc(BaseModel):
    schema_version: int


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_a_validation_error_names_the_file_and_keeps_the_detail(tmp_path: Path) -> None:
    path = _write(tmp_path / "bad.yaml", "schema_version: not-a-number\n")
    with pytest.raises(YamlModelError) as refused:
        load_yaml_model(path, _Doc)
    assert str(path) in str(refused.value)
    assert "schema_version" in str(refused.value)  # pydantic's field detail survives
    assert refused.value.__cause__ is not None
    assert isinstance(refused.value, ValueError)


def test_invalid_yaml_names_the_file(tmp_path: Path) -> None:
    path = _write(tmp_path / "broken.yaml", "a: [unclosed\n")
    with pytest.raises(YamlModelError, match="broken.yaml"):
        load_yaml_model(path, _Doc)


@pytest.mark.parametrize(
    ("text", "kind"), [("", "empty"), ("# only a comment\n", "empty"), ("- 1\n- 2\n", "a list")]
)
def test_an_empty_or_non_mapping_file_is_refused_not_defaulted(
    tmp_path: Path, text: str, kind: str
) -> None:
    """A truncated file must not read as a model of defaults (``dyno_profile``
    guarded this alone; it is now every caller's)."""
    path = _write(tmp_path / "doc.yaml", text)
    with pytest.raises(YamlModelError, match=f"doc.yaml is {kind}"):
        load_yaml_model(path, _Doc)


def test_a_missing_file_is_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_yaml_model(tmp_path / "absent.yaml", _Doc)


def test_sha256_is_over_the_raw_bytes(tmp_path: Path) -> None:
    raw = b"schema_version: 3\n# a comment the model never sees\nname: caf\xc3\xa9\n"
    path = tmp_path / "doc.yaml"
    path.write_bytes(raw)
    loaded = load_yaml_model(path, _Doc)
    assert loaded.sha256 == hashlib.sha256(raw).hexdigest()
    assert loaded.model == _Doc(schema_version=3, name="café")


def test_an_unchanged_file_returns_the_cached_instance(tmp_path: Path) -> None:
    path = _write(tmp_path / "doc.yaml", "schema_version: 1\n")
    first = load_yaml_model(path, _Doc)
    assert load_yaml_model(path, _Doc) is first
    assert load_yaml_model(tmp_path / "." / "doc.yaml", _Doc) is first  # resolved path


def test_the_cache_is_per_model_type(tmp_path: Path) -> None:
    path = _write(tmp_path / "doc.yaml", "schema_version: 1\n")
    doc = load_yaml_model(path, _Doc)
    other = load_yaml_model(path, _OtherDoc)
    assert type(other.model) is _OtherDoc
    assert type(doc.model) is _Doc
    assert doc.sha256 == other.sha256


def test_an_edited_file_is_reread(tmp_path: Path) -> None:
    path = _write(tmp_path / "doc.yaml", "schema_version: 1\n")
    first = load_yaml_model(path, _Doc)
    # Same size, new mtime: only the stat distinguishes it.
    path.write_text("schema_version: 2\n")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    second = load_yaml_model(path, _Doc)
    assert second is not first
    assert second.model.schema_version == 2
    assert second.sha256 != first.sha256
    # Same mtime, new size.
    stat = path.stat()
    path.write_text("schema_version: 22\n")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert load_yaml_model(path, _Doc).model.schema_version == 22


def test_a_same_size_rewrite_straight_after_a_load_is_not_served_stale(tmp_path: Path) -> None:
    """The racy-stat case, with no ``os.utime``: this kernel stamps mtime from a
    coarse clock (1987 of 2000 back-to-back same-size writes shared one), so
    ``(mtime, size)`` alone returned the old model and the old sha256."""
    path = tmp_path / "doc.yaml"
    for value in range(100, 300):
        path.write_text(f"schema_version: {value}\n")
        assert load_yaml_model(path, _Doc).model.schema_version == value
        raw = f"schema_version: {value + 1000}\n".encode()
        path.write_bytes(raw)  # same size as the text above
        loaded = load_yaml_model(path, _Doc)
        assert loaded.model.schema_version == value + 1000
        assert loaded.sha256 == hashlib.sha256(raw).hexdigest()


def test_a_same_size_rewrite_with_an_identical_stat_is_reread(tmp_path: Path) -> None:
    """The same race made deterministic: restore the first write's mtime."""
    path = _write(tmp_path / "doc.yaml", "schema_version: 1\n")
    first = load_yaml_model(path, _Doc)
    stat = path.stat()
    path.write_text("schema_version: 2\n")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert (path.stat().st_mtime_ns, path.stat().st_size) == (stat.st_mtime_ns, stat.st_size)
    second = load_yaml_model(path, _Doc)
    assert second.model.schema_version == 2
    assert second.sha256 != first.sha256


def test_a_settled_file_is_trusted_by_its_stat_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outside the racy window a hit costs one ``stat`` and reads no bytes."""
    path = _write(tmp_path / "doc.yaml", "schema_version: 1\n")
    old = path.stat().st_mtime_ns - 60 * 1_000_000_000
    os.utime(path, ns=(old, old))
    first = load_yaml_model(path, _Doc)

    def refuse(self: Path) -> bytes:
        raise AssertionError("a settled hit must not read the file")

    monkeypatch.setattr(Path, "read_bytes", refuse)
    assert load_yaml_model(path, _Doc) is first


def test_a_racy_entry_settles_once_the_window_has_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path / "doc.yaml", "schema_version: 1\n")
    first = load_yaml_model(path, _Doc)  # cached within the window of its mtime
    real_time_ns = time.time_ns
    later = real_time_ns() + 10 * 1_000_000_000
    monkeypatch.setattr(time, "time_ns", lambda: later)
    assert load_yaml_model(path, _Doc) is first  # verified by hash, now settled
    reads: list[Path] = []
    original = Path.read_bytes

    def counting(self: Path) -> bytes:
        reads.append(self)
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    assert load_yaml_model(path, _Doc) is first
    assert reads == []


def test_a_failure_is_not_cached(tmp_path: Path) -> None:
    path = _write(tmp_path / "doc.yaml", "")
    for _ in range(2):
        with pytest.raises(YamlModelError):
            load_yaml_model(path, _Doc)
    path.write_text("schema_version: 5\n")
    assert load_yaml_model(path, _Doc).model.schema_version == 5


def test_clear_forgets_every_entry(tmp_path: Path) -> None:
    path = _write(tmp_path / "doc.yaml", "schema_version: 1\n")
    first = load_yaml_model(path, _Doc)
    clear_yaml_model_cache()
    assert load_yaml_model(path, _Doc) is not first


# --- R26.3: the catalogs share the cache ---------------------------------------


def test_the_catalog_loaders_share_one_cached_model() -> None:
    assert load_did_map(DIDS_YAML) is load_did_map(DIDS_YAML)
    assert load_bindings(BINDINGS_YAML) is load_bindings(BINDINGS_YAML)


def _did_frame(hex_value: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": [datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)],
            "source": ["obd"],
            "channel": ["DID_1225"],
            "value_number": [None],
            "value_text": [hex_value],
            "unit": [None],
        }
    )


def test_an_edited_dids_yaml_is_picked_up_by_the_next_read_channels(tmp_path: Path) -> None:
    dids = tmp_path / "dids.yaml"
    original = DIDS_YAML.read_text()
    assert original.count("    scale: 0.005\n") >= 1
    dids.write_text(original)
    frame = _did_frame("01b8")  # 0x01b8 = 440 counts
    spec = ["did:ECM_AF_SENSOR_V_B1"]
    before = read_channels(frame, spec, bindings_path=BINDINGS_YAML, dids_path=dids)
    assert before["value"].iloc[0] == pytest.approx(2.20)

    # Rewrite the one DID's scale in place (same stat granularity as a real edit).
    marker = "name: ECM_AF_SENSOR_V_B1"
    head, tail = original.split(marker, 1)
    tail = tail.replace("scale: 0.005\n", "scale: 0.01\n", 1)
    assert tail != original.split(marker, 1)[1]
    dids.write_text(head + marker + tail)

    after = read_channels(frame, spec, bindings_path=BINDINGS_YAML, dids_path=dids)
    assert after["value"].iloc[0] == pytest.approx(4.40)

    # A spec that only the edit removes is refused by name on the next call.
    dids.write_text(
        original.replace("name: ECM_AF_SENSOR_V_B1", "name: ECM_AF_SENSOR_V_B1_RENAMED")
    )
    assert raw_read_filters(spec, bindings_path=BINDINGS_YAML, dids_path=dids) == []
