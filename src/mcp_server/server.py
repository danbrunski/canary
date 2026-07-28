"""Build the configured :class:`FastMCP` instance with all tools registered.

Tools live in :mod:`src.mcp_server.tools` (read) and
:mod:`src.mcp_server.write_tools` (write). This module is the assembly layer:
it constructs the shared :class:`DaemonClient`, :class:`TelemetryQuery`, and
:class:`MCPAuditLogger`, then registers thin closures that delegate to the
tool functions. Keeping the tools themselves dependency-free makes them
trivially unit-testable.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from pydantic import AnyHttpUrl

from src.mcp_server import resources as resource_handlers
from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.auth import StaticBearerVerifier
from src.mcp_server.client import DaemonClient
from src.mcp_server.config import MCPConfig, load_from_env
from src.mcp_server.parquet_query import TelemetryQuery
from src.mcp_server.tools import (
    analysis as analysis_tool,
)
from src.mcp_server.tools import (
    anomalies as anomalies_tool,
)
from src.mcp_server.tools import (
    behavior as behavior_tool,
)
from src.mcp_server.tools import (
    bindings as bindings_tool,
)
from src.mcp_server.tools import (
    binned_analysis as binned_analysis_tool,
)
from src.mcp_server.tools import (
    classification as classification_tool,
)
from src.mcp_server.tools import (
    digest as digest_tool,
)
from src.mcp_server.tools import (
    dtc as dtc_tool,
)
from src.mcp_server.tools import (
    jobs as jobs_tool,
)
from src.mcp_server.tools import (
    logs as logs_tool,
)
from src.mcp_server.tools import (
    monitors as monitors_tool,
)
from src.mcp_server.tools import (
    power as power_tool,
)
from src.mcp_server.tools import (
    replay as replay_tool,
)
from src.mcp_server.tools import (
    search as search_tool,
)
from src.mcp_server.tools import (
    service_health as service_health_tool,
)
from src.mcp_server.tools import (
    sessions as sessions_tool,
)
from src.mcp_server.tools import (
    status as status_tool,
)
from src.mcp_server.tools import (
    telemetry as telemetry_tool,
)
from src.mcp_server.tools import (
    trends as trends_tool,
)
from src.mcp_server.tools import (
    trip_summary as trip_summary_tool,
)
from src.mcp_server.tools import (
    tuning as tuning_tool,
)
from src.mcp_server.tools import (
    vehicle_ledger as vehicle_ledger_tool,
)
from src.mcp_server.write_tools import (
    can_activity as can_activity_write_tool,
)
from src.mcp_server.write_tools import (
    captures as captures_tool,
)
from src.mcp_server.write_tools import (
    classification as classification_write_tool,
)
from src.mcp_server.write_tools import (
    feedback as feedback_tool,
)
from src.mcp_server.write_tools import (
    jobs as jobs_write_tool,
)
from src.mcp_server.write_tools import (
    models as models_write_tool,
)
from src.mcp_server.write_tools import (
    obd_request as obd_request_write_tool,
)
from src.mcp_server.write_tools import (
    passive as passive_write_tool,
)
from src.mcp_server.write_tools import (
    power as power_write_tool,
)
from src.mcp_server.write_tools import (
    replay as replay_write_tool,
)
from src.mcp_server.write_tools import (
    sessions as sessions_write_tool,
)
from src.mcp_server.write_tools import (
    trigger_poll as trigger_poll_write_tool,
)
from src.mcp_server.write_tools import (
    tuning as tuning_write_tool,
)
from src.mcp_server.write_tools import (
    tuning_mode as tuning_mode_write_tool,
)
from src.mcp_server.write_tools import (
    vehicle_ledger as vehicle_ledger_write_tool,
)
from src.storage.catalog import SessionCatalog

logger = logging.getLogger(__name__)


def _resolve_storage_path(config: MCPConfig) -> Path | None:
    """Storage path from env, or ``None`` if unset (DuckDB tool stays disabled)."""
    if config.storage_path is not None:
        return config.storage_path
    return None


def _resolve_catalog(config: MCPConfig) -> SessionCatalog | None:
    """Session catalog for ``query_telemetry`` pushdown (Task 6.4a), or
    ``None`` when unset/never built. Only opened when the file already
    exists — the MCP server never creates a catalog just by starting up;
    that's ``scripts/catalog.py rebuild``'s job."""
    if config.catalog_db is None or not config.catalog_db.exists():
        return None
    return SessionCatalog(config.catalog_db)


def _build_auth(
    config: MCPConfig,
) -> tuple[AuthSettings | None, StaticBearerVerifier | None]:
    """Wire ``CANARY_CONTROL_TOKEN`` into FastMCP's bearer-auth middleware.

    Returns ``(None, None)`` when the token is unset so HTTP stays open for
    dev tunnelling and stdio remains unaffected either way. When the token
    is set, both HTTP and SSE endpoints reject requests lacking
    ``Authorization: Bearer <token>``.

    The synthetic issuer / resource URLs satisfy ``AuthSettings``' Pydantic
    validation but only surface in the ``WWW-Authenticate`` header on 401
    responses — we're a resource server, not an OAuth issuer.
    """
    if not config.control_token:
        return None, None
    # ``bind_host`` can be ``0.0.0.0``; fall back to a loopback literal for
    # the WWW-Authenticate hint since clients won't dial 0.0.0.0 anyway.
    host = config.bind_host if config.bind_host not in ("", "0.0.0.0") else "127.0.0.1"
    auth = AuthSettings(
        issuer_url=AnyHttpUrl("http://127.0.0.1/"),
        resource_server_url=AnyHttpUrl(f"http://{host}:{config.bind_port}/mcp"),
    )
    return auth, StaticBearerVerifier(config.control_token)


def build_server(config: MCPConfig | None = None) -> FastMCP:
    """Construct a :class:`FastMCP` with all CANary tools registered."""
    cfg = config or load_from_env()
    auth_settings, token_verifier = _build_auth(cfg)
    mcp = FastMCP(
        name="canary",
        instructions=(
            "CANary on-vehicle anomaly-detection daemon. Read tools surface "
            "current and historical telemetry, anomaly events, and model "
            "state. Write tools manage captures, tag events, and record "
            "feedback. All write tools are audit-logged with actor=mcp."
        ),
        host=cfg.bind_host,
        port=cfg.bind_port,
        # 0.0.0.0 binds to the LAN — TransportSecuritySettings warns about
        # that by default; we know and accept it. Auth is enforced via the
        # token verifier below when CANARY_CONTROL_TOKEN is set; stdio
        # ignores it (whoever spawns the process is already trusted).
        auth=auth_settings,
        token_verifier=token_verifier,
        # Opt-in (CANARY_MCP_STATELESS=1): no per-connection session id, so a
        # canary-mcp restart never strands a client on an invalidated
        # session. See MCPConfig.mcp_stateless / load_from_env's docstring.
        stateless_http=cfg.mcp_stateless,
    )

    client = DaemonClient(cfg.daemon_url, cfg.daemon_token)
    audit = MCPAuditLogger(cfg.audit_path)
    storage_path = _resolve_storage_path(cfg)
    query_runner: TelemetryQuery | None = (
        TelemetryQuery(storage_path, catalog=_resolve_catalog(cfg))
        if storage_path is not None
        else None
    )

    _register_read_tools(mcp, client, query_runner, cfg)
    _register_write_tools(mcp, client, audit)
    _register_resources(mcp, client, cfg)
    return mcp


def _register_read_tools(
    mcp: FastMCP,
    client: DaemonClient,
    query_runner: TelemetryQuery | None,
    cfg: MCPConfig,
) -> None:
    """Register all read tools as MCP tools."""

    @mcp.tool()
    async def get_status() -> dict[str, Any]:
        """Daemon liveness, loaded models, storage, anomaly emitter counters."""
        return await status_tool.get_status(client)

    @mcp.tool()
    async def get_config() -> dict[str, Any]:
        """Echo the daemon's loaded canary.yaml as JSON."""
        return await status_tool.get_config(client)

    @mcp.tool()
    async def get_power_status() -> dict[str, Any]:
        """UPS power state: SoC, watchdog armed/on-battery/grace, keep-alive hold."""
        return await power_tool.get_power_status(client)

    @mcp.tool()
    async def get_recent_anomalies(
        since: str | None = None,
        limit: int = 100,
        source: str | None = None,
        mode: str | None = None,
    ) -> dict[str, Any]:
        """Anomaly events since `since` (ISO-8601), optionally filtered by source/mode."""
        return await anomalies_tool.get_recent_anomalies(
            client, since=since, limit=limit, source=source, mode=mode
        )

    @mcp.tool()
    async def get_recent_telemetry(
        n: int = 100,
        channel: str | None = None,
        per_channel: bool = False,
    ) -> dict[str, Any]:
        """Last `n` telemetry rows; `channel` filters; `per_channel` returns latest-per-channel."""
        return await telemetry_tool.get_recent_telemetry(
            client, n=n, channel=channel, per_channel=per_channel
        )

    @mcp.tool()
    async def get_behavior_live(include_series: bool = False) -> dict[str, Any]:
        """Live per-model expected/actual/deviation/severity; rolling trail opt-in."""
        return await behavior_tool.get_behavior_live(client, include_series=include_series)

    @mcp.tool()
    async def get_behavior_models() -> dict[str, Any]:
        """Static behavior surfaces (the learned 'normal' maps)."""
        return await behavior_tool.get_behavior_models(client)

    @mcp.tool()
    async def get_bindings() -> dict[str, Any]:
        """Decoded CAN signal metadata from data/bindings.yaml."""
        return await bindings_tool.get_bindings(client)

    @mcp.tool()
    async def get_dids() -> dict[str, Any]:
        """Nissan Mode-22 DID catalog (schema_version + per-DID entries)."""
        return await bindings_tool.get_dids(client)

    @mcp.tool()
    async def get_obd_coverage() -> dict[str, Any]:
        """OBD coverage: which PIDs the ECU supports, which CANary decodes."""
        return await bindings_tool.get_obd_coverage(client)

    @mcp.tool()
    async def get_dtc() -> dict[str, Any]:
        """Cached stored/pending/permanent DTC report, MIL state, staleness."""
        return await dtc_tool.get_dtc(client)

    @mcp.tool()
    async def get_monitors() -> dict[str, Any]:
        """Cached Mode 06 on-board monitor sweep: per-cylinder misfire
        counts + raw monitor test results (value/min/max, pass/fail)."""
        return await monitors_tool.get_monitors(client)

    @mcp.tool()
    async def get_trends() -> dict[str, Any]:
        """Session-fingerprint drift: per-stat latest value, reference band,
        Mann-Kendall trend verdict, CUSUM alarm state."""
        return await trends_tool.get_trends(client)

    @mcp.tool()
    async def get_binned_analysis(
        value: str,
        x: str,
        y: str | None = None,
        x_bins: int = 12,
        y_bins: int | None = None,
        agg: str = "mean",
        session: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        """Bin a value channel by an x (and optional y) channel over a
        session/date scope — "trims by rpm×load" without hand-written SQL
        or a refit (Task 5.1). ``agg``: mean/median/p95/count."""
        return await binned_analysis_tool.get_binned_analysis(
            client,
            value=value,
            x=x,
            y=y,
            x_bins=x_bins,
            y_bins=y_bins,
            agg=agg,
            session=session,
            since=since,
            until=until,
        )

    @mcp.tool()
    async def list_sessions(
        since: str | None = None,
        until: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Sessions, optionally bounded by ISO start times and newest count."""
        return await sessions_tool.list_sessions(client, since=since, until=until, limit=limit)

    @mcp.tool()
    async def get_session(session_id: str) -> dict[str, Any]:
        """Detail for one session: tags, control.json, chunk count, metadata."""
        return await sessions_tool.get_session(client, session_id=session_id)

    @mcp.tool()
    async def get_session_timeseries(
        session_id: str,
        channels: list[str],
        resample_hz: float = 1.0,
    ) -> dict[str, Any]:
        """Aligned historical channels from a Pi session, without SSH/export.

        Use ``source:CHANNEL`` names such as ``obd:RPM`` and
        ``obd:ENGINE_LOAD``. One Hz is appropriate for a whole drive; use a
        higher rate only when resolving a short transient.
        """
        if not channels:
            return {"error": "channels must not be empty"}
        if not 0.1 <= resample_hz <= 20.0:
            return {"error": "resample_hz must be between 0.1 and 20.0"}
        return await sessions_tool.get_session_timeseries(
            client,
            session_id=session_id,
            channels=channels,
            resample_hz=resample_hz,
        )

    @mcp.tool()
    async def get_dataset_health() -> dict[str, Any]:
        """Corpus readiness: per-mode sample counts, gaps, last-seen times."""
        return await sessions_tool.get_dataset_health(client)

    @mcp.tool()
    async def get_training_runs() -> dict[str, Any]:
        """Training-run history (the latest record per run from the ledger)."""
        return await sessions_tool.get_training_runs(client)

    @mcp.tool()
    async def list_vehicle_events() -> dict[str, Any]:
        """Vehicle maintenance/intervention ledger, oldest-first (Task 3.3)."""
        return await vehicle_ledger_tool.list_vehicle_events(client)

    @mcp.tool()
    async def get_session_classification(session_id: str) -> dict[str, Any]:
        """Modes + events + disposition sidecar for one session.

        Returns the post-hoc classifier output: mode_segments (timeline of
        warmup/idle/city/highway/etc.), event_counts (cold_start,
        hard_accel, ...), and a disposition (ready/review/quarantine).
        404 when the sidecar hasn't been generated yet — call
        ``classify_session`` or run ``scripts/label_sessions.py``.
        """
        return await classification_tool.get_session_classification(client, session_id=session_id)

    @mcp.tool()
    async def get_trip_summary(session_id: str) -> dict[str, Any]:
        """Trip-summary sidecar for one session: duration, distance, fuel
        used, average economy, CO2, idle time/fraction, max/avg speed, 0-60
        and 1/4-mile if a clean launch was captured. Computed on demand from
        the stored session and cached after the first request.
        """
        return await trip_summary_tool.get_trip_summary(client, session_id=session_id)

    @mcp.tool()
    async def get_session_digest(session_id: str) -> dict[str, Any]:
        """End-of-drive digest for one session: duration, mode seconds,
        gated anomaly counts by source, top behavior-surface deviations,
        fingerprint deltas vs. the frozen reference epoch, new DTCs, and
        new vehicle-ledger events. Written by the trip-end hook (sustained
        GPS stop, or daemon shutdown as a fallback); computed on demand and
        cached if that hook hasn't fired yet for this session.
        """
        return await digest_tool.get_session_digest(client, session_id=session_id)

    @mcp.tool()
    async def get_corpus_mode_summary() -> dict[str, Any]:
        """Aggregate mode-coverage hours + event counts across all classified sessions.

        Answers "do I have enough warmup data to retrain?" — look at
        ``mode_seconds.warmup`` and ``event_counts.cold_start``.
        """
        return await classification_tool.get_corpus_mode_summary(client)

    @mcp.tool()
    async def list_events_by_kind(
        kind: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Corpus-wide event search through ``derived_tags.jsonl`` sidecars.

        ``kind`` filters to events of one type (``hard_accel``,
        ``cold_start``, ``cruise``, ``hard_brake``, ``idle_period``,
        ``stop``, ``wot_run``); ``None`` returns all kinds. Newest-first,
        capped at ``limit``.
        """
        return await classification_tool.list_events_by_kind(client, kind=kind, limit=limit)

    @mcp.tool()
    async def list_jobs() -> dict[str, Any]:
        """All known jobs (queued, running, completed, failed)."""
        return await jobs_tool.list_jobs(client)

    @mcp.tool()
    async def get_job(job_id: str) -> dict[str, Any]:
        """Status + result payload for a single job."""
        return await jobs_tool.get_job(client, job_id=job_id)

    @mcp.tool()
    async def get_job_logs(job_id: str) -> dict[str, Any]:
        """Captured stdout/stderr of a job."""
        return await jobs_tool.get_job_logs(client, job_id=job_id)

    @mcp.tool()
    async def get_tuning() -> dict[str, Any]:
        """Currently active alert-tuning config (thresholds, dedupe, etc.)."""
        return await tuning_tool.get_tuning(client)

    @mcp.tool()
    async def get_feedback() -> dict[str, Any]:
        """All recorded operator/MCP feedback labels + summary counts."""
        return await tuning_tool.get_feedback(client)

    @mcp.tool()
    def tail_daemon_log(
        n: int = 100,
        since: str | None = None,
        unit: str | None = None,
    ) -> dict[str, Any]:
        """Recent daemon log lines as parsed JSON records.

        Reads from systemd journal when available; falls back to a flat log
        file at $CANARY_MCP_LOG_FALLBACK otherwise. ``unit`` overrides the
        default ($CANARY_MCP_DAEMON_UNIT, "canary").
        """
        return logs_tool.tail_daemon_log(
            n=n,
            since=since,
            unit=unit or cfg.daemon_unit,
            fallback_path=cfg.log_fallback_path,
        )

    @mcp.tool()
    def get_service_health(units: list[str] | None = None) -> dict[str, Any]:
        """systemctl health (active/restart-count/uptime) for canary, canary-mcp,
        ups-shutdown — the crash-loop tell without SSH."""
        return service_health_tool.get_service_health(units)

    @mcp.tool()
    def search_anomaly_log(
        kind: str = "anomalies",
        since: str | None = None,
        until: str | None = None,
        contains: str | None = None,
        source: str | None = None,
        mode: str | None = None,
        min_score: float | None = None,
        max_score: float | None = None,
        affected_channel: str | None = None,
        session: str | None = None,
        action: str | None = None,
        result: str | None = None,
        actor: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Search anomaly history (kind='anomalies') or the MCP audit log (kind='audit').

        Anomaly filters: source (isolation_forest|lstm_autoencoder), mode,
        min_score / max_score, affected_channel, session. Audit filters:
        action (e.g. 'mcp.start_capture'), result ('ok'|'error'), actor.
        ``contains`` does a raw-text substring match for fields outside the
        structured filters. Results are newest-first, capped at ``limit``.
        """
        return search_tool.search_anomaly_log(
            kind=kind,
            anomalies_dir=cfg.anomalies_dir,
            audit_path=cfg.audit_path,
            since=since,
            until=until,
            contains=contains,
            source=source,
            mode=mode,
            min_score=min_score,
            max_score=max_score,
            affected_channel=affected_channel,
            session=session,
            action=action,
            result=result,
            actor=actor,
            limit=limit,
        )

    if cfg.signals_path is not None:
        signals_path = cfg.signals_path

        @mcp.tool()
        def signal_lookup(
            tag_label: str,
            can_id: int | None = None,
            top_k: int = 3,
        ) -> dict[str, Any]:
            """Fuzzy-match a tag label against the FSM signal catalog.

            ``can_id`` narrows the search to that module (e.g. 0x7E0 → ECM)
            for better precision. Returns top-k name matches with scores.
            """
            return analysis_tool.signal_lookup(
                signals_path, tag_label=tag_label, can_id=can_id, top_k=top_k
            )

    if query_runner is not None:

        @mcp.tool()
        def query_telemetry(
            sql: str,
            row_limit: int | None = None,
            session: str | None = None,
            since: str | None = None,
            until: str | None = None,
        ) -> dict[str, Any]:
            """Run a single SELECT/WITH against the telemetry Parquet view.

            View name is `telemetry` with columns timestamp, source, channel,
            value_number, value_text, value_bool, unit, metadata_json, and a
            derived `session` column. Non-SELECT statements are rejected.
            session/since/until scope which sessions' parquet back the view
            for this call (cheaper than scanning the whole corpus) — since/
            until are ISO-8601 or a bare session id, inclusive both ends.
            """
            return telemetry_tool.query_telemetry(
                query_runner,
                sql=sql,
                row_limit=row_limit,
                session=session,
                since=since,
                until=until,
            )
    else:
        logger.info(
            "query_telemetry disabled — CANARY_STORAGE_PATH unset; daemon HTTP "
            "tools remain available."
        )

    if cfg.config_path is not None and cfg.config_path.exists():
        config_path = cfg.config_path

        @mcp.tool()
        def get_behavior_deviations(
            session: str, mode: str | None = None, against: str | None = None
        ) -> dict[str, Any]:
            """Diff a stored session against the learned behavior surfaces (local,
            no daemon HTTP call) — did a mod/retrain/change actually move behavior?
            Pass ``against`` to A/B ``session`` directly against a second session
            id instead (Task 5.4: before-mod vs. after-mod, session-to-session)."""
            return behavior_tool.get_behavior_deviations(
                config_path, session=session, mode=mode, against=against
            )
    else:
        logger.info(
            "get_behavior_deviations disabled — CANARY_CONFIG unset or unreadable; "
            "get_behavior_live remains available for live state."
        )


def _register_write_tools(
    mcp: FastMCP,
    client: DaemonClient,
    audit: MCPAuditLogger,
) -> None:
    """Register all write tools as MCP tools."""

    @mcp.tool()
    async def start_capture(
        purpose: str,
        protocol: str = "drive",
        fields: dict[str, Any] | None = None,
        vehicle_state: str | None = None,
    ) -> dict[str, Any]:
        """Begin a labeled capture window on the current telemetry session.

        ``fields`` is free-form JSON kept with the capture (tune revision,
        ROM id, what the run is meant to prove).
        """
        return await captures_tool.start_capture(
            client,
            audit,
            purpose=purpose,
            protocol=protocol,
            fields=fields,
            vehicle_state=vehicle_state,
        )

    @mcp.tool()
    async def tag_current_capture(
        label: str,
        fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Drop a tag onto the active capture mid-drive."""
        return await captures_tool.tag_current_capture(client, audit, label=label, fields=fields)

    @mcp.tool()
    async def stop_capture(reason: str | None = None) -> dict[str, Any]:
        """Close the active capture; `reason` is recorded for later review."""
        return await captures_tool.stop_capture(client, audit, reason=reason)

    @mcp.tool()
    async def get_current_capture() -> dict[str, Any]:
        """Read the currently active capture window."""
        return await sessions_tool.get_current_capture(client)

    @mcp.tool()
    async def record_feedback(
        event: dict[str, Any],
        label: str,
        reason: str | None = None,
        driving_context: dict[str, Any] | None = None,
        labeled_by: str | None = None,
        hypothesis: str | None = None,
        clarifying_question: str | None = None,
        clarifying_answer: str | None = None,
    ) -> dict[str, Any]:
        """Record true_positive/false_positive/unknown verdict on an anomaly.

        Pass the whole `event` dict straight through from
        `get_recent_anomalies` — the store keys on the event, and these
        events have no id field.
        """
        # Cast at the boundary — FastMCP infers the schema from the str
        # annotation so the model passes a free string, and we validate it
        # against the Literal alias inside the tool body.
        if label not in ("true_positive", "false_positive", "unknown"):
            return {
                "ok": False,
                "error": ("label must be one of 'true_positive', 'false_positive', 'unknown'"),
            }
        # Narrow once for mypy — the runtime check above already proved the value.
        narrowed_label: feedback_tool.FeedbackLabel = label  # type: ignore[assignment]
        return await feedback_tool.record_feedback(
            client,
            audit,
            event=event,
            label=narrowed_label,
            reason=reason,
            driving_context=driving_context,
            labeled_by=labeled_by,
            hypothesis=hypothesis,
            clarifying_question=clarifying_question,
            clarifying_answer=clarifying_answer,
        )

    @mcp.tool()
    async def record_vehicle_event(
        occurred_at: str,
        kind: str,
        title: str,
        details: str = "",
        odometer_km: float | None = None,
        affected_channels: list[str] | None = None,
        corpus_boundary: bool = False,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """Log a maintenance/mod/repair/observation to the vehicle ledger (3.3).

        ``occurred_at`` is an ISO date (``YYYY-MM-DD``). Set
        ``corpus_boundary=True`` when the event changes what "normal" looks
        like (a mod or repair) so training can optionally start fresh from
        it (``--since-boundary``).

        Pass ``event_id`` (from `list_vehicle_events`) to CORRECT an existing
        entry: the ledger is append-only and reads collapse to the latest
        line per id, so the correction wins while the original stays on
        disk. Re-state the whole event, not just the changed field.
        """
        if kind not in ("maintenance", "mod", "repair", "observation", "dtc"):
            return {
                "ok": False,
                "error": (
                    "kind must be one of 'maintenance', 'mod', 'repair', 'observation', 'dtc'"
                ),
            }
        # Narrow once for mypy — the runtime check above already proved the value.
        narrowed_kind: vehicle_ledger_write_tool.VehicleEventKind = kind  # type: ignore[assignment]
        return await vehicle_ledger_write_tool.record_vehicle_event(
            client,
            audit,
            occurred_at=occurred_at,
            kind=narrowed_kind,
            title=title,
            details=details,
            odometer_km=odometer_km,
            affected_channels=affected_channels,
            corpus_boundary=corpus_boundary,
            event_id=event_id,
        )

    @mcp.tool()
    async def set_session_metadata(
        session_id: str,
        metadata: dict[str, Any],
        merge: bool = True,
    ) -> dict[str, Any]:
        """Attach / merge metadata onto a session."""
        return await sessions_write_tool.set_session_metadata(
            client, audit, session_id=session_id, metadata=metadata, merge=merge
        )

    @mcp.tool()
    async def pin_session(
        session_id: str,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Pin a session as a training candidate; pinned sessions survive sweeps."""
        return await sessions_write_tool.pin_session(
            client, audit, session_id=session_id, reason=reason, metadata=metadata
        )

    @mcp.tool()
    async def unpin_session(session_id: str) -> dict[str, Any]:
        """Remove the pin from a session — eligible for retention sweeps again."""
        return await sessions_write_tool.unpin_session(client, audit, session_id=session_id)

    @mcp.tool()
    async def start_job(kind: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Submit a job (corpus_report, retrain_if, retrain_lstm, ...)."""
        return await jobs_write_tool.start_job(client, audit, kind=kind, params=params)

    @mcp.tool()
    async def cancel_job(job_id: str) -> dict[str, Any]:
        """Best-effort cancel a running job."""
        return await jobs_write_tool.cancel_job(client, audit, job_id=job_id)

    @mcp.tool()
    async def set_tuning(
        config: dict[str, Any],
        change: str = "tuning update",
        by: str | None = None,
    ) -> dict[str, Any]:
        """Replace the alert-tuning config. The daemon auto-bumps version."""
        return await tuning_write_tool.set_tuning(
            client, audit, config=config, change=change, by=by
        )

    @mcp.tool()
    async def estimate_tuning(
        config: dict[str, Any],
        since: str | None = None,
        change: str = "tuning estimate",
        by: str | None = None,
    ) -> dict[str, Any]:
        """Before/after FP estimate for a proposed tuning over recent anomalies."""
        return await tuning_write_tool.estimate_tuning(
            client, audit, config=config, since=since, change=change, by=by
        )

    @mcp.tool()
    async def revert_tuning(version: int, by: str | None = None) -> dict[str, Any]:
        """Revert to a previous tuning version (from the audit log)."""
        return await tuning_write_tool.revert_tuning(client, audit, version=version, by=by)

    @mcp.tool()
    async def set_ups_keepalive(minutes: float) -> dict[str, Any]:
        """Set (minutes>0) or cancel (minutes=0) the UPS watchdog keep-alive hold."""
        return await power_write_tool.set_ups_keepalive(client, audit, minutes=minutes)

    @mcp.tool()
    async def can_activity(seconds: float, ids: list[int] | None = None) -> dict[str, Any]:
        """Capture N seconds of raw CAN traffic; per-ID frame rate + which
        byte positions changed.

        The RE workhorse: call it bracketing a physical action (press the
        brake, flip a switch) and `changed_byte_indices` on each id answers
        "which ID carries that signal," without an offline candump-and-diff
        round trip. Pure listen — no frames transmitted, no interaction with
        passive mode. `ids` restricts the capture to those arbitration ids;
        omit for the whole bus. `seconds` is capped at 30 server-side.
        """
        return await can_activity_write_tool.can_activity(client, audit, seconds=seconds, ids=ids)

    @mcp.tool()
    async def obd_request(
        service: int,
        identifier: int | None = None,
        header: int | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """One-shot read-only OBD/UDS request (Mode 01/02/03/06/07/09/0A/22).

        Ask for one specific service/identifier immediately instead of
        waiting for the poll scheduler's cadence (Mode 06/DTC sit on a
        5-minute tier). `header` overrides the default functional request id
        (0x7DF) for a physically-addressed request, e.g. 0x7E0 for the engine
        ECM or another module's tester address. `responded: false` means no
        answer in time, not necessarily "unsupported" — arm
        `set_passive_mode` first if another tool owns a diagnostic session.
        """
        return await obd_request_write_tool.obd_request(
            client,
            audit,
            service=service,
            identifier=identifier,
            header=header,
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool()
    async def trigger_poll(channels: list[str]) -> dict[str, Any]:
        """Force specific channels due on the OBD scheduler's next tick.

        Turns "wait up to 5 minutes" (DTC/Mode 06's tier) into "the answer is
        ready in a few seconds." Channel names match `get_status`'s
        `obd_scheduler.requests[].channel` (RPM, DTC_STORED,
        MONITOR_RESULTS, DID_1101, ...), case-insensitive; an unknown name
        is silently never matched. This only queues the request — read the
        result back with `get_status`/`get_dtc`/`get_recent_telemetry`/
        `query_telemetry` a few seconds later.
        """
        return await trigger_poll_write_tool.trigger_poll(client, audit, channels=channels)

    @mcp.tool()
    async def set_passive_mode(enabled: bool) -> dict[str, Any]:
        """Arm/disarm receive-only mode: daemon keeps logging but transmits no CAN.

        Use while another tool owns a diagnostic session (ECU reflash, actuator
        test). OBD channels go stale while armed — that is expected, and
        `get_status` reports `passive.enabled` so it is distinguishable from a
        real fault.
        """
        return await passive_write_tool.set_passive_mode(client, audit, enabled=enabled)

    @mcp.tool()
    async def set_tuning_mode(enabled: bool, channels: list[str] | None = None) -> dict[str, Any]:
        """Arm/disarm the focused high-rate tuning poll profile.

        Restricts Mode 01 to a small tuning channel set and suspends DID
        windows so each channel gets far more of the bus budget. Channels
        outside the set stop updating while armed - expected, not a fault;
        `get_status` reports `tuning_mode.active`.
        """
        return await tuning_mode_write_tool.set_tuning_mode(
            client, audit, enabled=enabled, channels=channels
        )

    @mcp.tool()
    async def replay_window(
        session_id: str,
        start: str | None = None,
        end: str | None = None,
        if_model: str | None = None,
        lstm_model: str | None = None,
        anomalies_dir: str | None = None,
        max_rows: int | None = None,
        max_windows: int | None = None,
        wait: bool = True,
        poll_interval: float = 2.0,
        timeout: float = 600.0,
    ) -> dict[str, Any]:
        """Replay a session window through IF + LSTM inference and diff vs history.

        Submits a daemon ``replay_window`` job (so heavy imports stay in the
        daemon process), optionally polls until it terminates, and returns
        the structured report with per-row IF scores, per-window LSTM
        scores + channel reconstruction errors, the historical anomaly
        events for the same window, and a match/replayed-only/historical-
        only diff. Set ``wait=False`` to submit and return the job id for
        async polling.
        """
        return await replay_tool.replay_window(
            client,
            audit,
            session_id=session_id,
            start=start,
            end=end,
            if_model=if_model,
            lstm_model=lstm_model,
            anomalies_dir=anomalies_dir,
            max_rows=max_rows,
            max_windows=max_windows,
            wait=wait,
            poll_interval=poll_interval,
            timeout=timeout,
        )

    @mcp.tool()
    async def upload_model_artifact(
        model_type: str,
        artifact_path: str,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        """Upload a trained model artifact (path local to the MCP host).

        ``artifact_path`` is read from the MCP server's filesystem,
        base64-encoded, and POSTed alongside ``manifest`` to
        ``/control/models/{model_type}/artifact``. Audited with file size +
        manifest; the base64 body itself is not logged.
        """
        from pathlib import Path

        return await models_write_tool.upload_model_artifact(
            client,
            audit,
            model_type=model_type,
            artifact_path=Path(artifact_path),
            manifest=manifest,
        )

    @mcp.tool()
    async def classify_session(session_id: str) -> dict[str, Any]:
        """Run the post-hoc classifier on one session; writes the sidecar.

        Idempotent — re-running overwrites the existing ``classification.json``
        with the current classifier version's output. Audit-logged.
        """
        return await classification_write_tool.classify_session(
            client, audit, session_id=session_id
        )

    @mcp.tool()
    async def start_replay(
        session_id: str,
        speed: float = 1.0,
        loop: bool = False,
    ) -> dict[str, Any]:
        """Replay a stored session into the live pipeline (isolated outputs).

        Parquet writes go to ``<storage>/_replay-<id>/`` and anomalies to
        ``<anomalies_dir>/_replay/<id>.jsonl`` — both excluded from training
        discovery and feedback. Use to evaluate IF + LSTM end-to-end on the
        Pi with no truck attached.
        """
        return await replay_write_tool.start_replay(
            client,
            audit,
            session_id=session_id,
            speed=speed,
            loop=loop,
        )

    @mcp.tool()
    async def stop_replay() -> dict[str, Any]:
        """Cancel the in-flight replay; daemon returns to live capture."""
        return await replay_write_tool.stop_replay(client, audit)

    @mcp.tool()
    async def get_replay_status() -> dict[str, Any]:
        """Snapshot of the current replay (idle, running, source, paths)."""
        return await replay_tool.get_replay_status(client)


def _register_resources(
    mcp: FastMCP,
    client: DaemonClient,
    cfg: MCPConfig,
) -> None:
    """Register URI-addressable resource endpoints.

    Resource bodies are returned as strings (JSON-pretty for snapshots,
    raw text for log files). FastMCP infers the URI template parameters
    from the function signature.
    """

    @mcp.resource("canary://config", mime_type="application/json")
    async def config_resource() -> str:
        """Daemon canary.yaml as pretty-printed JSON."""
        return await resource_handlers.read_config(client)

    @mcp.resource("canary://status", mime_type="application/json")
    async def status_resource() -> str:
        """Daemon status snapshot (storage, models, emitter counters)."""
        return await resource_handlers.read_status(client)

    @mcp.resource("canary://behavior-models", mime_type="application/json")
    async def behavior_resource() -> str:
        """Static behavior surfaces (the learned 'normal' maps)."""
        return await resource_handlers.read_behavior_models(client)

    @mcp.resource("canary://sessions/{session_id}", mime_type="application/json")
    async def session_resource(session_id: str) -> str:
        """One session's metadata + parquet inventory."""
        return await resource_handlers.read_session(client, session_id=session_id)

    if cfg.anomalies_dir is not None:
        anomalies_dir = cfg.anomalies_dir

        @mcp.resource("canary://anomalies/{date}", mime_type="application/x-ndjson")
        def anomalies_resource(date: str) -> str:
            """One day's anomaly events (YYYY-MM-DD; raw JSONL contents)."""
            return resource_handlers.read_anomalies_for_date(anomalies_dir, date=date)

    audit_path = cfg.audit_path

    @mcp.resource("canary://audit/mcp", mime_type="application/x-ndjson")
    def audit_resource() -> str:
        """Tail of the MCP-driven audit log."""
        return resource_handlers.read_audit_log(audit_path)


__all__ = ["build_server"]
