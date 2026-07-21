"""Async HTTP client for the local CANary daemon.

Thin wrapper around :class:`httpx.AsyncClient`. Centralises the base URL,
bearer-token header, and a small set of helper methods so each tool stays
~10 lines. Errors are surfaced as :class:`DaemonError` with the daemon's
JSON detail attached when present, so the MCP tool layer can return a
useful structured failure to the model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import httpx


class DaemonError(RuntimeError):
    """Raised when the daemon responds with non-2xx or is unreachable."""

    def __init__(self, message: str, *, status: int | None = None, detail: Any = None) -> None:
        # Embed the daemon's detail in the message so logs and audit entries
        # capture *why*, not just "HTTP 400".
        if detail is not None:
            message = f"{message}: {detail}"
        super().__init__(message)
        self.status = status
        self.detail = detail


class DaemonClient:
    """Async HTTP client bound to a single daemon base URL.

    Usage::

        async with DaemonClient(url, token) as client:
            status = await client.get_json("/status")
    """

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        timeout: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=timeout,
        )

    async def __aenter__(self) -> DaemonClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """``GET path`` → parsed JSON, or raise :class:`DaemonError`.

        The CANary daemon returns JSON objects from every documented endpoint
        (the dashboard wraps lists in ``{"events": [...], "count": N}`` and so
        on). If we ever surface a bare-array endpoint, this will need a
        sibling ``get_json_list``.
        """
        return cast(dict[str, Any], await self._request("GET", path, params=params))

    async def post_json(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """``POST path`` with a JSON body → parsed JSON or :class:`DaemonError`."""
        return cast(dict[str, Any], await self._request("POST", path, json_body=body))

    async def get_bytes(self, path: str, params: dict[str, Any] | None = None) -> bytes:
        """``GET path`` → raw response body (e.g. a session export zip)."""
        return cast(bytes, await self._request("GET", path, params=params, parse_json=False))

    async def download_file(
        self,
        path: str,
        dest: Path,
        *,
        resume: bool = True,
        chunk_size: int = 1 << 20,
    ) -> Path:
        """Stream ``GET path`` to ``dest`` via a resumable ``<dest>.part`` file.

        Companion session sync (6.34): archives can run tens/hundreds of MB
        over a laptop/Pi Wi-Fi link that drops mid-transfer. Streaming in
        ``chunk_size`` pieces keeps memory flat; leaving the partial
        ``<dest>.part`` file on disk on failure (instead of a full in-memory
        buffer that vanishes with the exception) means the *next* call
        (``resume=True``, the default) sends a ``Range`` header and continues
        from where it left off rather than re-downloading from byte 0. Only
        renamed to ``dest`` once the full body has landed, so a reader never
        observes a truncated file mid-download.
        """
        part = dest.with_name(dest.name + ".part")
        # At most one retry: a completed-but-stale `.part` (bigger than the
        # server's current file, or a server that ignores Range entirely)
        # gets one clean restart rather than looping forever.
        for attempt in range(2):
            offset = part.stat().st_size if resume and attempt == 0 and part.exists() else 0
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            mode = "ab" if offset else "wb"
            restart = False
            try:
                async with self._client.stream("GET", path, headers=headers) as response:
                    if response.status_code == 416:
                        # our partial file is already >= the current export;
                        # discard it and re-fetch from scratch.
                        part.unlink(missing_ok=True)
                        restart = True
                    elif response.status_code >= 400:
                        detail = (await response.aread()).decode("utf-8", errors="replace")
                        raise DaemonError(
                            f"GET {path} → HTTP {response.status_code}",
                            status=response.status_code,
                            detail=detail,
                        )
                    else:
                        if offset and response.status_code != 206:
                            # server ignored our Range header (e.g. a proxy
                            # that doesn't forward it) - the body is the
                            # whole file again, so don't append onto `part`.
                            mode = "wb"
                        with part.open(mode) as fh:
                            async for chunk in response.aiter_bytes(chunk_size):
                                fh.write(chunk)
            except httpx.HTTPError as exc:
                raise DaemonError(f"GET {path}: {exc}") from exc
            if not restart:
                break
        part.replace(dest)
        return dest

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        parse_json: bool = True,
    ) -> Any:
        try:
            response = await self._client.request(method, path, params=params, json=json_body)
        except httpx.HTTPError as exc:
            raise DaemonError(f"{method} {path}: {exc}") from exc

        if response.status_code >= 400:
            detail: Any
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            raise DaemonError(
                f"{method} {path} → HTTP {response.status_code}",
                status=response.status_code,
                detail=detail,
            )
        if not parse_json:
            return response.content
        if not response.content:
            return None
        return response.json()


__all__ = ["DaemonClient", "DaemonError"]
