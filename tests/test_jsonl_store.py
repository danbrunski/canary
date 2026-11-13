"""The locked JSONL primitives in ``src.jsonl_store`` (R24.9)."""

from __future__ import annotations

import json
import multiprocessing
import re
import threading
from pathlib import Path
from typing import Any

import pytest

from src.jsonl_store import (
    Appended,
    TailIndex,
    append_jsonl_line,
    append_jsonl_line_locked,
    append_jsonl_line_repairing,
    file_lock,
    path_lock,
)
from src.paths import REPO_ROOT

# -- path_lock -------------------------------------------------------------------


def test_one_lock_per_resolved_path(tmp_path: Path) -> None:
    path = tmp_path / "a" / "ledger.jsonl"
    (tmp_path / "a").mkdir()
    alias = tmp_path / "a" / ".." / "a" / "ledger.jsonl"
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "a")
    assert path_lock(path) is path_lock(path)
    assert path_lock(alias) is path_lock(path)
    assert path_lock(link / "ledger.jsonl") is path_lock(path)
    assert path_lock(tmp_path / "other.jsonl") is not path_lock(path)


def test_the_lock_is_reentrant(tmp_path: Path) -> None:
    """A store method that holds the lock calls another that takes it: two of the
    three registries this replaced were reentrant, and BrainJobStore.submit and
    the evidence ledger nest. A plain lock would deadlock on the first call."""
    lock = path_lock(tmp_path / "ledger.jsonl")
    done = threading.Event()

    def nest() -> None:
        with lock, path_lock(tmp_path / "ledger.jsonl"):
            done.set()

    worker = threading.Thread(target=nest, daemon=True)
    worker.start()
    worker.join(5)
    assert done.is_set()


def test_the_lock_excludes_other_threads(tmp_path: Path) -> None:
    lock = path_lock(tmp_path / "ledger.jsonl")
    taken = threading.Event()

    def take() -> None:
        with lock:
            taken.set()

    with lock:
        worker = threading.Thread(target=take, daemon=True)
        worker.start()
        worker.join(0.2)
        assert not taken.is_set()
    worker.join(5)
    assert taken.is_set()


def test_no_module_keeps_its_own_lock_registry() -> None:
    """The registry was copied three times; it lives in one place now."""
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in (REPO_ROOT / "src").rglob("*.py")
        if re.search(r"^def _lock_for\(", path.read_text(), re.MULTILINE)
    ]
    assert offenders == []


# -- append_jsonl_line_locked ------------------------------------------------------


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_the_bytes_written_are_the_unlocked_ones(tmp_path: Path) -> None:
    payload = {"b": 1, "a": {"z": 1, "y": [1, 2]}, "when": Path("x")}
    locked = tmp_path / "locked.jsonl"
    plain = tmp_path / "plain.jsonl"
    appended = append_jsonl_line_locked(locked, payload)
    append_jsonl_line(plain, payload)
    assert locked.read_bytes() == plain.read_bytes()
    assert locked.read_bytes() == (json.dumps(payload, sort_keys=True, default=str) + "\n").encode()
    assert appended == Appended(end=locked.stat().st_size, written=locked.stat().st_size)


def test_it_creates_the_directory_and_the_file(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "er" / "ledger.jsonl"
    append_jsonl_line_locked(path, {"n": 1})
    assert _lines(path) == [{"n": 1}]


@pytest.mark.parametrize("append", [append_jsonl_line_locked, append_jsonl_line_repairing])
def test_a_torn_tail_does_not_swallow_the_next_line(tmp_path: Path, append: Any) -> None:
    path = tmp_path / "ledger.jsonl"
    append(path, {"n": 1})
    with path.open("ab") as handle:
        handle.write(b'{"n": 2, "torn')  # a power cut mid-line
    before = path.stat().st_size
    appended = append(path, {"n": 3})
    assert path.read_bytes().endswith(b'torn\n{"n": 3}\n')
    assert appended.written == len(b'\n{"n": 3}\n')
    assert appended.end == before + appended.written == path.stat().st_size
    good = []
    for line in path.read_text().splitlines():
        try:
            good.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    assert good == [{"n": 1}, {"n": 3}]


@pytest.mark.parametrize("append", [append_jsonl_line_locked, append_jsonl_line_repairing])
def test_a_whole_tail_and_an_empty_file_get_no_separator(tmp_path: Path, append: Any) -> None:
    path = tmp_path / "ledger.jsonl"
    path.write_bytes(b"")
    append(path, {"n": 1})
    append(path, {"n": 2})
    assert path.read_bytes() == b'{"n": 1}\n{"n": 2}\n'


def test_threads_interleave_no_lines(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    threads, each = 8, 60
    filler = "x" * 20_000  # larger than a pipe write: a torn line would show

    def work(worker: int) -> None:
        for n in range(each):
            append_jsonl_line_locked(path, {"worker": worker, "n": n, "filler": filler})

    pool = [threading.Thread(target=work, args=(w,)) for w in range(threads)]
    for thread in pool:
        thread.start()
    for thread in pool:
        thread.join()
    records = _lines(path)  # every line parses whole
    assert len(records) == threads * each
    for worker in range(threads):
        assert [r["n"] for r in records if r["worker"] == worker] == list(range(each))


def _append_in_process(path: str, worker: int, count: int) -> None:
    for n in range(count):
        append_jsonl_line_locked(Path(path), {"worker": worker, "n": n, "filler": "y" * 20_000})


def test_processes_interleave_no_lines(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    context = multiprocessing.get_context("spawn")
    procs = [context.Process(target=_append_in_process, args=(str(path), w, 25)) for w in range(3)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(60)
        assert proc.exitcode == 0
    assert len(_lines(path)) == 75


def test_the_repairing_append_runs_inside_the_file_lock(tmp_path: Path) -> None:
    """flock is per open file description: a second flock on another handle in
    the same process blocks forever, which is why the evidence ledger holds
    ``file_lock`` and appends with the unlocking variant."""
    path = tmp_path / "ledger.jsonl"
    done = threading.Event()

    def work() -> None:
        with file_lock(path):
            append_jsonl_line_repairing(path, {"n": 1})
            with path_lock(path):  # the in-process lock is reentrant
                append_jsonl_line_repairing(path, {"n": 2})
        done.set()

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(5)
    assert done.is_set()
    assert _lines(path) == [{"n": 1}, {"n": 2}]


def test_the_file_lock_holds_other_threads_out(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    entered = threading.Event()

    def other() -> None:
        append_jsonl_line_locked(path, {"n": "other"})
        entered.set()

    with file_lock(path):
        worker = threading.Thread(target=other, daemon=True)
        worker.start()
        worker.join(0.2)
        assert not entered.is_set()
    worker.join(5)
    assert entered.is_set()


# -- TailIndex ---------------------------------------------------------------------


def _append(path: Path, data: bytes) -> None:
    with path.open("ab") as handle:
        handle.write(data)


def test_a_missing_file_reads_as_a_reset_with_nothing(tmp_path: Path) -> None:
    read = TailIndex().read_new(tmp_path / "nope.jsonl")
    assert read.reset and read.lines == []


def test_only_the_appended_lines_are_returned(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    index = TailIndex()
    path.write_bytes(b"one\ntwo\n")
    first = index.read_new(path)
    assert first.reset and first.lines == [b"one", b"two"]
    assert index.read_new(path).lines == [] and not index.read_new(path).reset
    _append(path, b"three\n")
    second = index.read_new(path)
    assert not second.reset and second.lines == [b"three"]
    assert index.offset == path.stat().st_size


def test_a_torn_tail_is_held_until_it_is_finished(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    index = TailIndex()
    path.write_bytes(b"one\ntw")
    assert index.read_new(path).lines == [b"one"]
    assert index.offset == 4
    assert index.read_new(path).lines == []  # still torn, offset unmoved
    assert index.offset == 4
    _append(path, b"o\nthree\n")
    assert index.read_new(path).lines == [b"two", b"three"]


def test_a_shorter_file_is_read_whole(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    index = TailIndex()
    path.write_bytes(b"one\ntwo\nthree\n")
    index.read_new(path)
    path.write_bytes(b"x\n")  # same inode, truncated and rewritten
    read = index.read_new(path)
    assert read.reset and read.lines == [b"x"]


def test_a_replaced_file_is_read_whole(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    index = TailIndex()
    path.write_bytes(b"one\n")
    index.read_new(path)
    replacement = tmp_path / "new.jsonl"
    replacement.write_bytes(b"AAA\nBBB\n")  # longer, another inode
    replacement.replace(path)
    read = index.read_new(path)
    assert read.reset and read.lines == [b"AAA", b"BBB"]


def test_a_deleted_file_resets(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    index = TailIndex()
    path.write_bytes(b"one\n")
    index.read_new(path)
    path.unlink()
    assert index.read_new(path).reset
    assert index.offset == 0 and index.identity is None
    path.write_bytes(b"one\n")
    assert index.read_new(path).lines == [b"one"]


def test_an_in_place_rewrite_of_the_same_length_needs_verify_bytes(tmp_path: Path) -> None:
    """Same inode, not shorter: only the byte comparison can tell."""
    plain, verified = TailIndex(), TailIndex(verify_bytes=True)
    path = tmp_path / "log.jsonl"
    path.write_bytes(b"alpha\nbeta\n")
    plain.read_new(path)
    verified.read_new(path)
    with path.open("r+b") as handle:  # rewritten in place
        handle.write(b"ALPHA\nBETA\n")
    assert not plain.read_new(path).reset
    read = verified.read_new(path)
    assert read.reset and read.lines == [b"ALPHA", b"BETA"]


def test_verify_bytes_keeps_an_untouched_file_incremental(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    index = TailIndex(verify_bytes=True)
    path.write_bytes(b"one\n" * 3000)  # larger than the head and tail windows
    index.read_new(path)
    _append(path, b"more\n")
    read = index.read_new(path)
    assert not read.reset and read.lines == [b"more"]


def test_it_can_skip_past_a_line_it_appended_itself(tmp_path: Path) -> None:
    for verify in (False, True):
        path = tmp_path / f"own-{verify}.jsonl"
        index = TailIndex(verify_bytes=verify)
        appended = append_jsonl_line_repairing(path, {"n": 1})
        assert index.accept_own_append(path, appended)  # the append created the file
        assert index.offset == appended.end
        appended = append_jsonl_line_repairing(path, {"n": 2})
        assert index.accept_own_append(path, appended)
        assert index.read_new(path).lines == []  # nothing left to parse
        _append(path, b'{"n": 3}\n')
        assert index.read_new(path).lines == [b'{"n": 3}']


def test_it_will_not_skip_a_line_when_another_writer_got_in_between(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    index = TailIndex()
    append_jsonl_line_repairing(path, {"n": 1})
    index.read_new(path)
    _append(path, b'{"foreign": true}\n')
    appended = append_jsonl_line_repairing(path, {"n": 2})
    assert not index.accept_own_append(path, appended)
    assert index.read_new(path).lines == [b'{"foreign": true}', b'{"n": 2}']


def test_it_will_not_skip_a_line_that_repaired_a_torn_tail(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    index = TailIndex()
    path.write_bytes(b'{"n": 1}\n{"n": 2')
    index.read_new(path)
    appended = append_jsonl_line_repairing(path, {"n": 3})
    assert not index.accept_own_append(path, appended)
    assert index.read_new(path).lines == [b'{"n": 2', b'{"n": 3}']


def test_an_existing_file_is_not_mistaken_for_one_the_append_created(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    path.write_bytes(b'{"n": 0}\n')
    index = TailIndex()  # never read
    appended = append_jsonl_line_repairing(path, {"n": 1})
    assert not index.accept_own_append(path, appended)
    assert index.read_new(path).lines == [b'{"n": 0}', b'{"n": 1}']
