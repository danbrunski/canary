"""Shared fakes for real objects (R07.5).

Each stands in for one real type and says which; the builders that produce
test *data* live in ``tests/builders.py``. Where the real type is cheap, the
fake is the real type (:func:`lstm_result` builds a real
``SequenceInferenceResult``; :func:`completed_process` a real
``subprocess.CompletedProcess``), so a field added to it cannot be silently
missing from the stand-in.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import can
import httpx
import pandas as pd

from src.analysis.digest import SessionDigest
from src.collection import lsm6dsox as lsm
from src.config import CanaryConfig
from src.mcp_server.client import DaemonClient
from src.models.inference import SequenceInferenceResult
from src.models.manager import LoadedModel, SwapAttempt
from src.models.manifest import ModelManifestCommon

# ---------------------------------------------------------------------------
# detectors
# ---------------------------------------------------------------------------


class FakeIsolationForest:
    """A trained IF artifact that scores every row the same way.

    ``calibrated`` decides whether the ``*_calibrated_threshold`` /
    ``*_calibrated_anomaly`` columns are emitted. The daemon treats a row
    without them as an uncalibrated mode, which is *non-emitting* at serve
    (A02.3), so the 13 hand-written copies that disagreed on this were not
    all exercising the path their names claimed. Say which one a test means.
    """

    mode_column = "operating_mode"

    def __init__(
        self,
        *,
        feature_columns: tuple[str, ...] = ("obd:RPM",),
        score: float = 0.9,
        label: int = -1,
        calibrated: bool = True,
        model_mode: str = "all",
        model_version: str = "fake_if_v1",
    ) -> None:
        self.feature_columns = feature_columns
        self._score = score
        self._label = label
        self._calibrated = calibrated
        self._model_mode = model_mode
        self.manifest = type("M", (), {"model_version": model_version})()

    def score(self, frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.copy()
        out["isolation_forest_score"] = self._score
        out["isolation_forest_label"] = self._label
        out["isolation_forest_model_mode"] = self._model_mode
        out["isolation_forest_missing_features"] = False
        if self._calibrated:
            out["isolation_forest_calibrated_threshold"] = 0.5
            out["isolation_forest_calibrated_anomaly"] = self._label == -1
        return out


def fake_if_model(
    *, label: int = -1, calibrated: bool = True, **kwargs: Any
) -> FakeIsolationForest:
    """A :class:`FakeIsolationForest`; ``label=-1`` flags every row."""
    return FakeIsolationForest(label=label, calibrated=calibrated, **kwargs)


class StubModelManager:
    """A ``ModelManager`` that serves one in-memory artifact and never swaps."""

    def __init__(
        self,
        artifact: Any,
        *,
        model_type: str = "isolation_forest",
        model_version: str | None = None,
    ) -> None:
        version = cast(str, model_version or getattr(artifact.manifest, "model_version", "fake"))
        self._loaded = LoadedModel(
            artifact=artifact,
            manifest=ModelManifestCommon(
                artifact_format_version=1,
                model_type=model_type,
                model_version=version,
                created_at="now",
                training_data_hash="h",
                input_rows=1,
            ),
            artifact_path=Path("/nonexistent/fake-artifact"),
            manifest_path=Path("/nonexistent/fake-artifact.manifest.json"),
            manifest_mtime=0.0,
        )

    @property
    def current(self) -> LoadedModel[Any]:
        return self._loaded

    def poll(self) -> SwapAttempt:
        return SwapAttempt(kind="no_change")


def lstm_result(**overrides: Any) -> SequenceInferenceResult:
    """A real ``SequenceInferenceResult`` (a frozen dataclass) flagging ``obd:RPM``.

    Three hand-rolled ``_FakeLSTMResult`` classes each carried a different
    subset of its attributes; building the real type means a new field has a
    default or fails here, once.
    """
    fields: dict[str, Any] = {
        "model_version": "lstm_v1",
        "timestamp": "2026-05-13T12:00:00+00:00",
        "session": "2026-05-13T120000",
        "window_start_timestamp": "2026-05-13T11:59:50+00:00",
        "window_end_timestamp": "2026-05-13T12:00:00+00:00",
        "global_score": 5.0,
        "global_threshold": 3.0,
        "channel_scores": {"obd:RPM": 5.0},
        "channel_thresholds": {"obd:RPM": 3.0},
        "anomalous_channels": ("obd:RPM",),
        "is_anomaly": True,
        "latency_ms": 4.5,
    }
    fields.update(overrides)
    return SequenceInferenceResult(**fields)


# ---------------------------------------------------------------------------
# CAN buses
# ---------------------------------------------------------------------------


class CannedResponseBus:
    """Replays canned ISO-TP frames from ``0x7E8``, whatever is sent.

    For client-level tests of one exchange (a multi-frame reassembly, a
    refusal, a timeout); anything with request-dependent answers or timing
    is :class:`src.validation.scripted_ecu.ScriptedEcu`'s job.
    """

    def __init__(self, frames: list[bytes]) -> None:
        self.sent: list[can.Message] = []
        self._frames = list(frames)

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        self.sent.append(msg)

    def recv(self, timeout: float | None = None) -> can.Message | None:
        if not self._frames:
            return None
        return can.Message(arbitration_id=0x7E8, data=self._frames.pop(0), is_extended_id=False)

    def shutdown(self) -> None:  # pragma: no cover - interface completeness
        pass


# ---------------------------------------------------------------------------
# processes and clients
# ---------------------------------------------------------------------------


def completed_process(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    """What a stubbed ``subprocess.run`` returns."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


async def daemon_client_with_handler(handler: Any) -> DaemonClient:
    """A ``DaemonClient`` whose HTTP calls go to ``handler`` (an ``httpx.MockTransport``).

    Still swaps the private ``_client``; a ``transport=`` constructor kwarg on
    ``DaemonClient`` would remove that (R07.5 remainder, ``src/mcp_server``).
    """
    client = DaemonClient("http://daemon")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://daemon", transport=httpx.MockTransport(handler)
    )
    return client


# ---------------------------------------------------------------------------
# trip-end digest
# ---------------------------------------------------------------------------


def slow_digest(seconds: float, calls: list[bool]) -> Callable[..., SessionDigest]:
    """A ``compute_digest`` stand-in that sleeps ``seconds`` and appends to
    ``calls`` whether it ran on a thread with a running event loop."""

    def fake(session_dir: Path, config: CanaryConfig, **_: Any) -> SessionDigest:
        try:
            asyncio.get_running_loop()
            calls.append(True)
        except RuntimeError:
            calls.append(False)
        time.sleep(seconds)
        return SessionDigest(
            schema_version=1,
            computed_at="2026-05-20T18:05:00+00:00",
            session_id=session_dir.name,
            duration_seconds=1.0,
            mode_seconds={},
            anomaly_counts_by_source={},
            top_behavior_deviations=[],
            fingerprint_top_movers=[],
            new_dtcs=[],
            new_ledger_events=[],
        )

    return fake


# ---------------------------------------------------------------------------
# LSM6DSOX IMU (src.collection.lsm6dsox)
# ---------------------------------------------------------------------------


def lsm_fifo_word(tag: int, counter: int, data: bytes) -> bytes:
    assert len(data) == 6
    return bytes([tag << 3 | (counter & 0x3) << 1]) + data


def lsm_vec(x: int, y: int, z: int) -> bytes:
    return b"".join(v.to_bytes(2, "little", signed=True) for v in (x, y, z))


def lsm_stamp(counter: int, ticks: int) -> bytes:
    return lsm_fifo_word(lsm.TAG_TIMESTAMP, counter, ticks.to_bytes(4, "little") + b"\0\0")


def lsm_fifo_instants(
    first_counter: int,
    n: int,
    *,
    stamp_every: int = 8,
    ticks0: int = 1000,
    ticks_per: float = 384.0,
) -> bytes:
    """``n`` instants of gyro+accel, with a timestamp word every ``stamp_every``."""
    out = b""
    for i in range(n):
        counter = (first_counter + i) % 4
        if i % stamp_every == 0:
            out += lsm_stamp(counter, round(ticks0 + i * ticks_per))
        out += lsm_fifo_word(lsm.TAG_GYRO, counter, lsm_vec(100 + i, 0, -100))
        out += lsm_fifo_word(lsm.TAG_ACCEL, counter, lsm_vec(0, 0, 4098))  # 4098 * 0.244 mg = 1.0 g
    return out


class FakeLsmBus:
    """Registers as a dict, a FIFO as a byte string, and a log of writes."""

    def __init__(self, fifo: bytes = b"", *, who: int = 0x6C, status2: int = 0) -> None:
        self.regs: dict[int, int] = {lsm.WHO_AM_I: who, lsm.CTRL3_C: 0, lsm.INTERNAL_FREQ_FINE: 0}
        self.fifo = fifo
        self.status2 = status2
        self.timestamp = 5000
        self.writes: list[tuple[int, int]] = []
        self.closed = False

    def read(self, register: int, length: int) -> bytes:
        if register == lsm.FIFO_STATUS1:
            words = len(self.fifo) // 7
            return bytes([words & 0xFF, (words >> 8) & 0x3 | self.status2])
        if register == lsm.TIMESTAMP0:
            return self.timestamp.to_bytes(4, "little")
        if register == lsm.FIFO_DATA_OUT_TAG:
            out, self.fifo = self.fifo[:length], self.fifo[length:]
            return out
        return bytes([self.regs.get(register + i, 0) for i in range(length)])

    def write(self, register: int, value: int) -> None:
        self.writes.append((register, value))
        if register == lsm.CTRL3_C:
            value &= ~lsm.CTRL3_SW_RESET  # reset completes immediately
        self.regs[register] = value

    def write_block(self, register: int, data: bytes) -> None:
        for offset, value in enumerate(data):
            self.write(register + offset, value)

    def close(self) -> None:
        self.closed = True
