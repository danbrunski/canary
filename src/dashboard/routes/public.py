from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from fastapi import FastAPI, HTTPException, Query

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.feedback import FeedbackStore, annotate_events_with_feedback
from src.analysis.gps_tracker import GpsTracker
from src.analysis.llm_assist import LlmAssistCardStore, card_to_dict
from src.config import CanaryConfig
from src.control.dtc_state import DtcState
from src.control.monitor_state import MonitorState
from src.control.replay_control import ReplayController
from src.control.sequence_detector import SequenceDetectorController
from src.dashboard.behavior_live import BehaviorLiveState
from src.dashboard.readers import (
    echo_config,
    read_accepted_baselines,
    read_anomalies_since,
    read_behavior_models,
    read_bindings_for_display,
    read_can_coverage,
    read_latest_per_channel,
    read_latest_telemetry_rows,
    read_obd_coverage,
)
from src.dashboard.status import build_status
from src.decode.accepted_baselines import DEFAULT_ACCEPTED_BASELINES_PATH
from src.models.health import HealthMonitor

if TYPE_CHECKING:  # decode.dids is imported lazily inside the route bodies
    from src.daemon.inference_metrics import InferenceTimingRecorder
    from src.decode.dids import DidMap


def register_public_routes(
    app: FastAPI,
    *,
    config: CanaryConfig,
    anomalies_dir: Path,
    models_dir: Path | None,
    emitter: AnomalyEventEmitter | None,
    feedback_store: FeedbackStore | None,
    behavior_models_path: Path,
    bindings_path: Path,
    dids_path: Path,
    test_profiles_dir: Path,
    replay_controller: ReplayController | None,
    health_monitor: HealthMonitor | None,
    behavior_live: BehaviorLiveState | None,
    dtc_state: DtcState | None = None,
    monitor_state: MonitorState | None = None,
    gps_tracker: GpsTracker | None = None,
    sequence_detector_controller: SequenceDetectorController | None = None,
    accepted_baselines_path: Path = DEFAULT_ACCEPTED_BASELINES_PATH,
    storage_writer: Any = None,
    inference_timing: InferenceTimingRecorder | None = None,
) -> None:
    @app.get("/status")
    def get_status() -> dict[str, Any]:
        payload = build_status(
            config,
            models_dir=models_dir,
            emitter=emitter,
            health_monitor=health_monitor,
            replay_controller=replay_controller,
            gps_tracker=gps_tracker,
            storage_writer=storage_writer,
            inference_timing=inference_timing,
        ).to_dict()
        if replay_controller is not None:
            payload["replay"] = replay_controller.status().to_dict()
        if sequence_detector_controller is not None:
            payload["sequence_detector"] = sequence_detector_controller.status().to_dict()
        # 2.3 — "N accepted-baseline overrides active", always visible; the
        # StatusStrip reads `.active`, the expandable detail reads
        # `.overrides` (each with provenance + `review_by` + `expired`).
        # Re-read every request, same posture as /models/behavior — the
        # file is tiny and an operator's edit should show up without a
        # daemon restart.
        payload["accepted_baseline_overrides"] = read_accepted_baselines(accepted_baselines_path)
        return payload

    @app.get("/config")
    def get_config() -> dict[str, Any]:
        return echo_config(config)

    @app.get("/anomalies")
    def get_anomalies(
        since: str | None = Query(default=None),
        source: str | None = Query(default=None),
        mode: str | None = Query(default=None),
        limit: int | None = Query(default=None, ge=1),
    ) -> dict[str, Any]:
        try:
            events = read_anomalies_since(
                anomalies_dir, since=since, source=source, mode=mode, limit=limit
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"bad `since`: {exc}") from exc
        # Join the operator's verdict onto each event so the timeline can
        # show "you marked this" without a second round-trip (Task 6.24).
        if feedback_store is not None:
            events = annotate_events_with_feedback(events, feedback_store)
        return {"events": events, "count": len(events)}

    @app.get("/telemetry/latest")
    def get_telemetry_latest(
        n: int = Query(default=100, ge=1, le=10_000),
        per_channel: bool = Query(default=False),
    ) -> dict[str, Any]:
        rows = (
            read_latest_per_channel(config.collection.storage_path)
            if per_channel
            else read_latest_telemetry_rows(config.collection.storage_path, n=n)
        )
        # Mode-22 polls are stored raw (``obd:DID_<hex>`` + a hex payload);
        # decode them here so this seed carries the same ``did:<NAME>`` rows
        # the live stream publishes. Without it a page load shows no DIDs
        # until the next poll window — up to a minute of blank gauges,
        # since the DID session is mutually exclusive with Mode 01 and so
        # runs in windows rather than continuously.
        rows = rows + _decoded_did_rows(rows)
        return {"rows": rows, "count": len(rows)}

    @app.get("/models/behavior")
    def get_behavior_models() -> dict[str, Any]:
        # Re-read on every request so a `scripts/refit_behavior_models.py`
        # run lands in the dashboard without a daemon restart. Cheap — the
        # file is < 100 KB even with a full surface catalog.
        return read_behavior_models(behavior_models_path)

    @app.get("/models/behavior/live")
    def get_behavior_live(
        series: bool = Query(default=True),
    ) -> dict[str, Any]:
        # Live "you are here" overlay for Models▸Behavior: per-model
        # expected/actual/deviation/severity + a short server-side trail,
        # fed by the daemon's inference loop. Empty (not an error) when no
        # daemon is wired — the static surfaces still render standalone.
        # series=false drops the rolling trail (the bulk of the payload) for
        # callers that only need the latest snapshot.
        if behavior_live is None:
            return {"observed": 0, "timestamp": None, "operating_mode": None, "models": {}}
        return behavior_live.snapshot(include_series=series)

    @app.get("/bindings")
    def get_bindings() -> dict[str, Any]:
        # Re-read per request so a hand-edit to bindings.yaml (during RE)
        # surfaces in the dashboard immediately. The file is small.
        return read_bindings_for_display(bindings_path)

    def _polled_did_names(did_map: DidMap) -> list[str]:
        """Catalog names for the configured `obd_dids`, which may be tokens
        of either form (a name or a bare 4-char hex id)."""
        names = {entry.name for entry in did_map.dids}
        by_id = {entry.did: entry.name for entry in did_map.dids}
        polled: list[str] = []
        for token in config.collection.obd_dids:
            stripped = token.strip()
            if stripped in names:
                polled.append(stripped)
                continue
            try:
                resolved = by_id.get(int(stripped, 16))
            except ValueError:
                continue
            if resolved is not None:
                polled.append(resolved)
        return polled

    def _decoded_did_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """``did:<NAME>`` rows decoded from the raw ``DID_<hex>`` rows in `rows`."""
        from src.decode.dids import (
            DID_SOURCE,
            decode_raw_did_reading,
            did_entries_by_hex,
            load_did_map,
        )

        raw = [r for r in rows if str(r.get("channel", "")).startswith("DID_")]
        if not raw or not dids_path.exists():
            return []
        by_hex = did_entries_by_hex(load_did_map(dids_path))
        decoded: list[dict[str, Any]] = []
        for row in raw:
            hit = decode_raw_did_reading(by_hex, str(row["channel"]), row.get("value"))
            if hit is None:
                continue
            decoded.append(
                {
                    "timestamp": row.get("timestamp"),
                    "source": DID_SOURCE,
                    "channel": hit.entry.name,
                    "value": hit.value,
                    "unit": hit.unit,
                }
            )
        return decoded

    @app.get("/dids")
    def get_dids() -> dict[str, Any]:
        # Read-only Nissan Mode-22 DID catalog for the 6.16 workbench.
        # Re-read per request like /bindings so a hand-edit during RE shows
        # up immediately; empty doc when the file is absent.
        from src.decode.dids import load_did_map

        if not dids_path.exists():
            return {"schema_version": None, "vehicle": {}, "access": None, "dids": [], "polled": []}
        did_map = load_did_map(dids_path)
        payload = did_map.model_dump(mode="json")
        # Which catalogued DIDs the daemon actually polls (`obd_dids`). The
        # catalog is the whole RE record, so most entries are characterized
        # but not polled — without this the dashboard can't tell "not asked
        # for" from "asked for, no answer", and reads every unpolled entry
        # as a failure.
        payload["polled"] = _polled_did_names(did_map)
        return payload

    @app.get("/test-profiles")
    def get_test_profiles() -> dict[str, Any]:
        # Read-only catalog for the Test panel, scanned per request so a
        # dropped-in / hand-edited profile YAML appears without a daemon
        # restart. Unauthenticated like /dids + /bindings (no secrets;
        # listing profiles must work before the control token is entered).
        from src.control.test_profiles import load_test_profiles

        return load_test_profiles(test_profiles_dir).model_dump(mode="json")

    @app.get("/obd/coverage")
    def get_obd_coverage() -> dict[str, Any]:
        # Read-only honesty surface (Task 6.3 / Risk R7): which public PIDs
        # the ECU supports, which CANary decodes, which are undecoded gaps.
        # Also lists custom PIDs (Task 2.8a) — no secrets in `custom_pids.yaml`,
        # same public/unauthenticated posture as the DID catalog below.
        return read_obd_coverage(
            config.collection.storage_path,
            custom_pids_path=config.daemon.custom_pids,
        )

    @app.get("/can/coverage")
    def get_can_coverage() -> dict[str, Any]:
        # Static catalog of decodable broadcast-CAN signals (bindings + DBC)
        # with their source CAN IDs. The CAN tab overlays live values and
        # discovered/undecoded IDs from the telemetry stream client-side.
        return read_can_coverage(bindings_path)

    @app.get("/obd/dtc")
    def get_obd_dtc() -> dict[str, Any]:
        # Task 1.5: the cached DtcReport (Task 1.4) the periodic Mode
        # 03/07/0A poll (Task 1.3) keeps current — stored/pending/permanent
        # codes, MIL state, and staleness. Empty-but-shaped when no poll has
        # landed yet (daemon just started, or DTC polling is disabled).
        if dtc_state is None:
            return {
                "captured_at": None,
                "stored": [],
                "pending": [],
                "permanent": [],
                "mil_on": None,
                "dtc_count": 0,
                "freeze_frame": None,
                "stale": True,
            }
        return dtc_state.snapshot()

    @app.get("/obd/monitors")
    def get_obd_monitors() -> dict[str, Any]:
        # Task 3.1: the cached MonitorState (Mode 06 on-board monitor
        # results — misfire counts + monitor value/min/max margins) the
        # periodic Mode 06 poll keeps current. Empty-but-shaped when no poll
        # has landed yet, same convention as /obd/dtc above.
        if monitor_state is None:
            return {
                "captured_at": None,
                "misfire_counts": {},
                "results": [],
                "stale": True,
            }
        return monitor_state.snapshot()

    @app.get("/analysis/binned")
    def get_binned_analysis(
        value: str = Query(...),
        x: str = Query(...),
        y: str | None = Query(default=None),
        x_bins: int = Query(default=12, ge=2, le=64),
        y_bins: int | None = Query(default=None, ge=2, le=64),
        agg: str = Query(default="mean"),
        session: str | None = Query(default=None),
        since: str | None = Query(default=None),
        until: str | None = Query(default=None),
        units: str = Query(default="stored"),
    ) -> dict[str, Any]:
        # Task 5.1: ad-hoc binned aggregation ("trims by rpm×load") over the
        # DuckDB parquet layer — no hand-written SQL, no refit. Public/
        # read-only like the other stored-data reads on this router (trip
        # summary, DTC, monitors) — no secrets, just historical telemetry.
        from src.analysis.binned_analysis import Aggregation
        from src.control.binned_analysis import binned_analysis_response

        if agg not in ("mean", "median", "p95", "count"):
            raise HTTPException(status_code=400, detail=f"unsupported agg: {agg!r}")
        # `units="engineering"` rescales decoded: channels via bindings.yaml;
        # they are stored raw on purpose, so binning them as-is works in counts.
        if units not in ("stored", "engineering"):
            raise HTTPException(status_code=400, detail=f"unsupported units: {units!r}")
        return binned_analysis_response(
            config,
            value=value,
            x=x,
            y=y,
            x_bins=x_bins,
            y_bins=y_bins,
            agg=cast("Aggregation", agg),
            session=session,
            since=since,
            until=until,
            units=units,
        )

    @app.get("/analysis/llm-assist/cards")
    def get_llm_assist_cards() -> dict[str, Any]:
        # Task S.2: public/read-only like the routes above — the dashboard
        # card renderer's feature-flagged endpoint. Disabled (the default;
        # no producer exists yet, Task S.1) is an explicit, expected state,
        # not an error: the response just omits `cards` so the renderer's
        # absent-state branch is a plain `enabled` check, no error handling.
        if not config.llm_assist.enabled:
            return {"enabled": False}
        store = LlmAssistCardStore(config.llm_assist.cards_path)
        return {"enabled": True, "cards": [card_to_dict(card) for card in store.cards()]}

    @app.get("/sessions/{session_id}/trip-summary")
    def get_session_trip_summary(session_id: str) -> dict[str, Any]:
        # Task 1.5: public read like /obd/dtc — no session-close hook exists
        # yet (eval-longitudinal-and-dtc.md Phase 5.4), so this computes the
        # trip_summary.json sidecar (Task 1.4) on first request and serves
        # the cached copy after.
        from src.control.trip_summary import get_trip_summary

        result = get_trip_summary(config, session_id)
        if result is None:
            raise HTTPException(
                status_code=404, detail=f"no trip summary for session {session_id!r}"
            )
        return result
