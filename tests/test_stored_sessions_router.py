"""R03.7: the per-session routes both apps serve, declared once over a store.

The daemon and the companion each registered read/metadata/pin/unpin/delete/
export/checksum and called a hook-parameterized ``services.*_response``. The
per-app differences are now a :class:`SessionStore`; these tests pin that each
difference still lands where the old hook put it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.control.sessions import write_session_zip
from src.dashboard.routes.stored_sessions import (
    SessionStore,
    build_stored_session_router,
    session_export_checksum,
)


def _session(root: Path) -> Path:
    session = root / "2026-05-17T120000"
    session.mkdir(parents=True)
    (session / "manifest.json").write_text(json.dumps({"started_at": "now"}))
    return session


def _resolver(session: Path) -> Any:
    def resolve(session_id: str) -> Path:
        if session_id != session.name or not session.is_dir():
            raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
        return session

    return resolve


def _client(store: SessionStore) -> TestClient:
    app = FastAPI()
    app.include_router(build_stored_session_router(store, lambda: None))
    return TestClient(app)


def test_session_export_checksum_matches_the_zip_hash(tmp_path: Path) -> None:
    session = _session(tmp_path)
    result = session_export_checksum(session.name, session)
    assert result["session_id"] == session.name
    tmp_zip = tmp_path / "check.zip"
    write_session_zip(session, tmp_zip)
    assert result["sha256"] == hashlib.sha256(tmp_zip.read_bytes()).hexdigest()
    assert result["size_bytes"] == tmp_zip.stat().st_size


def test_metadata_carries_a_session_only_when_the_store_reindexes(tmp_path: Path) -> None:
    """Daemon shape: no ``session`` key. Companion shape: the re-indexed session."""
    session = _session(tmp_path)
    daemon = _client(SessionStore(resolve=_resolver(session), describe=dict, export_prefix="x-"))
    body = daemon.post(
        f"/control/sessions/{session.name}/metadata", json={"metadata": {"route": "mesa"}}
    ).json()
    assert "session" not in body
    assert body["metadata"]["route"] == "mesa"

    reindexed: list[Path] = []

    def reindex(path: Path) -> dict[str, Any]:
        reindexed.append(path)
        return {"refreshed": True}

    companion = _client(
        SessionStore(resolve=_resolver(session), describe=dict, export_prefix="x-", reindex=reindex)
    )
    body = companion.post(
        f"/control/sessions/{session.name}/metadata", json={"metadata": {"route": "mesa"}}
    ).json()
    assert body["session"] == {"refreshed": True}
    assert reindexed == [session]


def test_pin_serializes_through_the_reindex_when_there_is_one(tmp_path: Path) -> None:
    session = _session(tmp_path)
    store = SessionStore(
        resolve=_resolver(session),
        describe=dict,
        export_prefix="x-",
        reindex=lambda path: {"session_id": path.name, "reimported": True},
    )
    body = (
        _client(store)
        .post(
            f"/control/sessions/{session.name}/pin",
            json={"reason": "good drive", "metadata": {"terrain": "city"}},
        )
        .json()
    )
    assert body["session"] == {"session_id": session.name, "reimported": True}
    assert body["metadata"]["terrain"] == "city"

    # Without one, the written session is re-summarized from disk.
    plain = SessionStore(resolve=_resolver(session), describe=dict, export_prefix="x-")
    body = _client(plain).post(f"/control/sessions/{session.name}/unpin").json()
    assert body["session"]["session_id"] == session.name


def test_delete_runs_the_guard_first_and_the_index_drop_after(tmp_path: Path) -> None:
    session = _session(tmp_path)
    calls: list[str] = []
    published: list[tuple[str, dict[str, Any]]] = []
    store = SessionStore(
        resolve=_resolver(session),
        describe=dict,
        export_prefix="x-",
        guard_delete=lambda sid: calls.append(f"guard:{sid}"),
        after_delete=lambda sid: calls.append(f"after:{sid}:{session.exists()}"),
        on_write=lambda event, payload: published.append((event, payload)),
    )
    body = _client(store).delete(f"/control/sessions/{session.name}").json()
    assert body == {"session_id": session.name, "deleted": True, "force": False}
    assert calls == [f"guard:{session.name}", f"after:{session.name}:False"]
    assert published == [("session.delete", body)]


def test_a_guard_refusal_leaves_the_session_on_disk(tmp_path: Path) -> None:
    session = _session(tmp_path)

    def refuse(_sid: str) -> None:
        raise HTTPException(status_code=409, detail="cannot delete the active capture session")

    store = SessionStore(
        resolve=_resolver(session), describe=dict, export_prefix="x-", guard_delete=refuse
    )
    response = _client(store).delete(f"/control/sessions/{session.name}")
    assert response.status_code == 409
    assert session.is_dir()


@pytest.mark.parametrize(
    ("method", "path", "body", "event"),
    [
        ("post", "/metadata", {"metadata": {"a": 1}}, "session.metadata"),
        ("post", "/pin", {"reason": "r"}, "session.pin"),
        ("post", "/unpin", None, "session.unpin"),
    ],
)
def test_every_write_is_published_with_its_payload(
    tmp_path: Path, method: str, path: str, body: dict[str, Any] | None, event: str
) -> None:
    session = _session(tmp_path)
    published: list[tuple[str, dict[str, Any]]] = []
    store = SessionStore(
        resolve=_resolver(session),
        describe=dict,
        export_prefix="x-",
        on_write=lambda name, payload: published.append((name, payload)),
    )
    response = getattr(_client(store), method)(f"/control/sessions/{session.name}{path}", json=body)
    assert response.status_code == 200
    assert published == [(event, response.json())]


def test_read_export_and_checksum_resolve_through_the_store(tmp_path: Path) -> None:
    session = _session(tmp_path)
    store = SessionStore(
        resolve=_resolver(session),
        describe=lambda sid: {"session_id": sid, "described": True},
        export_prefix="x-",
    )
    client = _client(store)
    assert client.get(f"/control/sessions/{session.name}").json() == {
        "session": {"session_id": session.name, "described": True}
    }
    exported = client.get(f"/control/sessions/{session.name}/export")
    assert exported.status_code == 200
    checksum = client.get(f"/control/sessions/{session.name}/export/checksum").json()
    assert checksum["sha256"] == hashlib.sha256(exported.content).hexdigest()
    assert client.get("/control/sessions/nope/export").status_code == 404
