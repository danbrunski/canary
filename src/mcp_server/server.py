"""Build the configured :class:`FastMCP` instance with all tools registered.

Tools live in :mod:`src.mcp_server.tools` (read) and
:mod:`src.mcp_server.write_tools` (write). This module is the assembly layer:
it constructs the shared :class:`DaemonClient`, :class:`TelemetryQuery`, and
:class:`MCPAuditLogger`, then registers the tools. Pure delegations are rows
in :data:`READ_DELEGATIONS` / :data:`WRITE_DELEGATIONS` (schema and
description come from the delegate); wrappers with real logic or conditional
registration are hand-written closures. Keeping the tools themselves
dependency-free makes them trivially unit-testable.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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
from src.mcp_server.tools import brain as brain_tool
from src.mcp_server.tools import capture_profiles as capture_profiles_tool
from src.mcp_server.tools import (
    classification as classification_tool,
)
from src.mcp_server.tools import (
    did_sweep as did_sweep_tool,
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
    rom_editor as rom_editor_tool,
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
    tuning_analysis as tuning_analysis_tool,
)
from src.mcp_server.tools import (
    vehicle_ledger as vehicle_ledger_tool,
)
from src.mcp_server.write_tools import (
    bus_budget as bus_budget_write_tool,
)
from src.mcp_server.write_tools import (
    can_activity as can_activity_write_tool,
)
from src.mcp_server.write_tools import capture_profiles as capture_profiles_write_tool
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
    tuning_capture as tuning_capture_write_tool,
)
from src.mcp_server.write_tools import (
    tuning_mode as tuning_mode_write_tool,
)
from src.mcp_server.write_tools import (
    vehicle_ledger as vehicle_ledger_write_tool,
)
from src.storage.catalog import SessionCatalog
from src.storage.telemetry_query import TelemetryQuery

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Delegation:
    """One MCP tool that does nothing but forward to its delegate.

    The delegate is a ``tools/`` or ``write_tools/`` function whose name is
    the tool name, whose leading positional parameters are the injected
    ``client`` (and ``audit`` when ``needs_audit``), and whose keyword-only
    parameters are the tool's inputs. FastMCP infers the input schema and the
    description from the delegate itself, so its docstring is the one source
    of the tool's text. Wrappers with real logic (validation, branching,
    config-derived arguments) or conditional registration stay hand-written
    below.
    """

    delegate: Callable[..., Awaitable[dict[str, Any]]]
    needs_audit: bool = False


#: Read tools that are pure delegations (no audit).
READ_DELEGATIONS: tuple[Delegation, ...] = (
    Delegation(status_tool.get_status),
    Delegation(status_tool.get_config),
    Delegation(power_tool.get_power_status),
    Delegation(anomalies_tool.get_recent_anomalies),
    Delegation(telemetry_tool.get_recent_telemetry),
    Delegation(behavior_tool.get_behavior_live),
    Delegation(behavior_tool.get_behavior_models),
    Delegation(bindings_tool.get_bindings),
    Delegation(bindings_tool.get_dids),
    Delegation(bindings_tool.get_obd_coverage),
    Delegation(dtc_tool.get_dtc),
    Delegation(monitors_tool.get_monitors),
    Delegation(trends_tool.get_trends),
    Delegation(sessions_tool.list_sessions),
    Delegation(capture_profiles_tool.list_capture_profiles),
    Delegation(sessions_tool.get_session),
    Delegation(sessions_tool.get_dataset_health),
    Delegation(binned_analysis_tool.get_binned_analysis),
    Delegation(sessions_tool.get_training_runs),
    Delegation(vehicle_ledger_tool.list_vehicle_events),
    Delegation(classification_tool.get_session_classification),
    Delegation(trip_summary_tool.get_trip_summary),
    Delegation(digest_tool.get_session_digest),
    Delegation(classification_tool.get_corpus_mode_summary),
    Delegation(classification_tool.list_events_by_kind),
    Delegation(jobs_tool.list_jobs),
    Delegation(jobs_tool.get_job),
    Delegation(jobs_tool.get_job_logs),
    Delegation(did_sweep_tool.diff_did_snapshots),
    Delegation(tuning_tool.get_tuning),
    Delegation(tuning_tool.get_feedback),
    Delegation(brain_tool.get_brain_status),
    Delegation(brain_tool.query_memory),
    Delegation(brain_tool.resolve_evidence),
    Delegation(brain_tool.get_investigation_card),
    Delegation(brain_tool.get_twin_session_evaluation),
    Delegation(brain_tool.get_ecu_application_model),
    Delegation(brain_tool.list_brain_jobs),
    Delegation(brain_tool.get_brain_job),
    Delegation(rom_editor_tool.preview_rom_edit),
    Delegation(rom_editor_tool.simulate_rom_edit),
)

#: Write-side tools that are pure delegations; ``needs_audit`` injects the audit logger.
WRITE_DELEGATIONS: tuple[Delegation, ...] = (
    Delegation(captures_tool.tag_current_capture, needs_audit=True),
    Delegation(sessions_tool.get_current_capture),
    Delegation(sessions_write_tool.set_session_metadata, needs_audit=True),
    Delegation(sessions_write_tool.pin_session, needs_audit=True),
    Delegation(sessions_write_tool.unpin_session, needs_audit=True),
    Delegation(jobs_write_tool.start_job, needs_audit=True),
    Delegation(jobs_write_tool.cancel_job, needs_audit=True),
    Delegation(tuning_write_tool.set_tuning, needs_audit=True),
    Delegation(tuning_write_tool.estimate_tuning, needs_audit=True),
    Delegation(tuning_write_tool.revert_tuning, needs_audit=True),
    Delegation(power_write_tool.set_ups_keepalive, needs_audit=True),
    Delegation(can_activity_write_tool.can_activity, needs_audit=True),
    Delegation(obd_request_write_tool.obd_request, needs_audit=True),
    Delegation(trigger_poll_write_tool.trigger_poll, needs_audit=True),
    Delegation(passive_write_tool.set_passive_mode, needs_audit=True),
    Delegation(bus_budget_write_tool.set_bus_budget, needs_audit=True),
    Delegation(tuning_mode_write_tool.set_tuning_mode, needs_audit=True),
    Delegation(tuning_capture_write_tool.arm_tuning_capture, needs_audit=True),
    Delegation(tuning_capture_write_tool.get_capture_quality),
    Delegation(tuning_capture_write_tool.disarm_tuning_capture, needs_audit=True),
    Delegation(replay_tool.replay_window, needs_audit=True),
    Delegation(classification_write_tool.classify_session, needs_audit=True),
    Delegation(replay_write_tool.start_replay, needs_audit=True),
    Delegation(replay_write_tool.stop_replay, needs_audit=True),
    Delegation(replay_tool.get_replay_status),
)


def bind_delegation(
    entry: Delegation, client: DaemonClient, audit: MCPAuditLogger | None
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """The delegate with ``client``/``audit`` bound, shaped for FastMCP.

    ``functools.wraps`` carries the delegate's name and docstring; the
    signature (and resolved annotations) drop the injected leading
    parameters, so they can never surface as tool inputs. A delegate whose
    leading parameters are not exactly the injected ones is refused at
    registration rather than bound to the wrong argument.
    """
    delegate = entry.delegate
    injected: tuple[Any, ...] = (client, audit) if entry.needs_audit else (client,)
    names = ("client", "audit") if entry.needs_audit else ("client",)
    if entry.needs_audit and audit is None:
        raise ValueError(f"{delegate.__name__} needs the audit logger")
    signature = inspect.signature(delegate, eval_str=True)
    params = list(signature.parameters.values())
    leading = tuple(param.name for param in params[: len(names)])
    if leading != names or any(
        param.kind is not inspect.Parameter.KEYWORD_ONLY for param in params[len(names) :]
    ):
        raise TypeError(
            f"{delegate.__name__}: expected positional {names} then keyword-only "
            f"inputs, got {signature}"
        )
    exposed = params[len(names) :]

    @functools.wraps(delegate)
    async def tool(**kwargs: Any) -> dict[str, Any]:
        return await delegate(*injected, **kwargs)

    tool.__signature__ = signature.replace(parameters=exposed)  # type: ignore[attr-defined]
    tool.__annotations__ = {param.name: param.annotation for param in exposed} | {
        "return": signature.return_annotation
    }
    return tool


def _register_delegations(
    mcp: FastMCP,
    entries: tuple[Delegation, ...],
    client: DaemonClient,
    audit: MCPAuditLogger | None,
) -> None:
    for entry in entries:
        mcp.tool()(bind_delegation(entry, client, audit))


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
    _register_delegations(mcp, READ_DELEGATIONS, client, None)

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
            value_number, value_text, value_bool, unit, payload, metadata_json,
            and a derived `session` column. Non-SELECT statements are rejected.
            session/since/until scope which sessions' parquet back the view
            for this call (cheaper than scanning the whole corpus) — since/
            until are ISO-8601 or a bare session id, inclusive both ends.
            `decoded` rows are decoded on read from the raw CAN frames with the
            current bindings (P02.8: none is stored since 2026-09-26, and an old
            session's stored ones are never shown); a query that asks for them
            carries a note, and an empty_reason when it finds nothing.
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

        @mcp.tool()
        def get_delivery_error(
            session: str,
            measured_spec: str = "obd:O2_WR_LAMBDA_S1",
            min_load: float | None = None,
        ) -> dict[str, Any]:
            """Measured-vs-commanded AFR at enrichment for one stored session,
            with the methodology gates applied and reported (local, no daemon
            call). Target and measured reads are paired by acquisition time
            within the vehicle's declared separation, and only steady-target
            samples count; closed loop, fuel cut and every other exclusion is
            a named gate, and results are band-averaged over rpm so uneven
            coverage cannot masquerade as an effect. ``measured_spec`` must be
            a measured source the vehicle's lambda_delivery.yaml declares: the
            ECM lambda and A/F-voltage paths disagree by ~3.8% at WOT and
            neither is established as truth. ``min_load`` defaults to the
            vehicle profile's WOT load floor."""
            return tuning_analysis_tool.get_delivery_error(
                config_path,
                session=session,
                measured_spec=measured_spec,
                min_load=min_load,
            )

        @mcp.tool()
        def compare_tuning_sessions(
            session: str,
            against: str,
            value: str,
            axes: list[str] | None = None,
            floor_channel: str | None = None,
            unsafe_axes: bool = False,
            conditions: str | None = None,
        ) -> dict[str, Any]:
            """A/B two stored sessions on a matched grid (local). Refuses
            calculated load as an axis — it is derived from BFS, so it moves
            when fuelling moves; grid on rpm x MAF or rpm x BFS. Pass
            ``floor_channel`` with ``conditions`` (the regime this comparison
            was made under) to quote each delta against the measured same-tune
            noise floor, below which nothing is distinguishable; a floor
            measured under other conditions is refused, not quoted."""
            return tuning_analysis_tool.compare_tuning_sessions(
                config_path,
                session=session,
                against=against,
                value=value,
                axes=tuple(axes) if axes else ("obd:RPM", "obd:MAF"),
                floor_channel=floor_channel,
                unsafe_axes=unsafe_axes,
                conditions=conditions,
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
    _register_delegations(mcp, WRITE_DELEGATIONS, client, audit)

    @mcp.tool()
    async def start_capture(
        purpose: str | None = None,
        protocol: str = "drive",
        fields: dict[str, Any] | None = None,
        vehicle_state: str | None = None,
        profile: str | None = None,
        tune_revision: str | None = None,
    ) -> dict[str, Any]:
        """Start a named capture profile or a legacy labeled capture window.

        Pass ``profile`` for an isolated collection session. Without it,
        ``purpose`` starts the existing tag window on the current session.
        """
        if profile is not None:
            return await capture_profiles_write_tool.start_capture(
                client, audit, profile=profile, tune_revision=tune_revision
            )
        if purpose is None:
            raise ValueError("purpose is required when profile is omitted")
        return await captures_tool.start_capture(
            client,
            audit,
            purpose=purpose,
            protocol=protocol,
            fields=fields,
            vehicle_state=vehicle_state,
        )

    @mcp.tool()
    async def stop_capture(reason: str | None = None) -> dict[str, Any]:
        """Stop the active profile capture, otherwise close the tag window.

        Raises when there is no tag window to close (HTTP 409) rather than
        answering with a null capture — a restart drops the in-memory window,
        so "stopped" and "there was nothing to stop" are genuinely different
        answers. The refusal names that cause.
        """
        # `view=compact` carries `capture_profile.active`, which is all this
        # branch needs; the full payload costs 1.3-2.6 s on the Pi.
        status = await status_tool.get_status(client, view="compact")
        if (status.get("capture_profile") or {}).get("active"):
            return await capture_profiles_write_tool.stop_capture(client, audit)
        return await captures_tool.stop_capture(client, audit, reason=reason)

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
        tires: dict[str, Any] | None = None,
        intervention: str | None = None,
    ) -> dict[str, Any]:
        """Log a maintenance/mod/repair/observation to the vehicle ledger (3.3).

        ``occurred_at`` is an ISO date (``YYYY-MM-DD``). Set
        ``corpus_boundary=True`` when the event changes what "normal" looks
        like (a mod or repair) so training can optionally start fresh from
        it (``--since-boundary``).

        Pass ``event_id`` (from `list_vehicle_events`) to CORRECT an existing
        entry: the correction replaces it on disk, and the original is gone.
        Re-state the whole event, not just the changed field. Removing an
        entry outright is ``DELETE /control/vehicle-ledger/{id}``.

        ``tires`` records a tire fact the tire-wear trend reads:
        ``{"action": "tread_measured", "tread_depth_mm": 9.5}`` (a gauge
        reading on the driven axle, the baseline for estimated tread depth),
        or ``{"action": "rotated" | "replaced" | "pressure_changed"}``.

        ``intervention`` classifies an event that changed the vehicle or its
        instruments: ``"hardware"``, ``"fuel"`` or ``"instrumentation"``
        (E01.4 investigation memory). A correction re-states it too, or the
        classification is erased with the rest of the old entry.
        """
        if intervention is not None and intervention not in (
            "hardware",
            "fuel",
            "instrumentation",
        ):
            return {
                "ok": False,
                "error": "intervention must be one of 'hardware', 'fuel', 'instrumentation'",
            }
        narrowed_intervention: vehicle_ledger_write_tool.InterventionKind | None = intervention  # type: ignore[assignment]
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
            tires=tires,
            intervention=narrowed_intervention,
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
