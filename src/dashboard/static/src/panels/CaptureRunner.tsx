// Running a capture (R08.6, the runner half of CaptureProfilesPanel):
// Engine Tuning → Logging. Pick a saved profile, start, pause, stop, watch it
// record. Editing profiles is CaptureProfileEditor's job (System); the runner
// has no draft, so it can only ever launch a saved profile by name.

import { useCallback, useEffect, useRef, useState } from "react";

import { pauseProfileCapture, startProfileCapture, stopProfileCapture } from "../api/http";
import { Notice } from "../components/Notice";
import { Panel } from "../components/Panel";
import { TableScroll } from "../components/TableScroll";
import { useAsyncAction } from "../hooks/useAsyncAction";
import { useCaptureCatalog } from "../hooks/useCaptureCatalog";
import { useNow } from "../hooks/useNow";
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import { streamKey } from "../lib/romMaps";
import { effectiveTargetHz } from "../lib/scheduler";
import { buttonClass, cn, inputClass } from "../styles";
import type { CaptureProfileState, CaptureQuality, StatusResponse } from "../types";
import { formatReading } from "../util/format";
import { CapturePlanSummary, CollectionStatus, Field, ProfilePicker } from "./captureProfileParts";

export type CapturePhase =
  "idle" | "starting" | "stopping" | "pausing" | "resuming" | "recording" | "paused";

/** What the controls are doing right now. An in-flight transition is a
 *  distinct state from the settled ones: "Starting…" is the answer to the
 *  complaint that nothing happens when Start is pressed. `busy` is the
 *  `useAsyncAction` label of the action in flight. */
export function capturePhase(
  busy: string | null,
  active: Pick<CaptureProfileState, "active" | "paused"> | null,
): CapturePhase {
  if (busy === "capture start") return "starting";
  if (busy === "capture stop") return "stopping";
  if (busy === "capture pause") return "pausing";
  if (busy === "capture resume") return "resuming";
  if (!active?.active) return "idle";
  return active.paused ? "paused" : "recording";
}

/** Start / Pause / Stop on a phone (P5.1): full-height, sharing the row, so
 *  they can be hit from the driver's seat. */
const RUN_BUTTON_PHONE = "max-[600px]:min-h-11 max-[600px]:flex-1";

const TRANSITIONS: ReadonlySet<CapturePhase> = new Set([
  "starting",
  "stopping",
  "pausing",
  "resuming",
]);

const PHASE_TEXT: Record<Exclude<CapturePhase, "idle">, string> = {
  starting: "Starting…",
  stopping: "Stopping…",
  pausing: "Pausing…",
  resuming: "Resuming…",
  recording: "Recording",
  paused: "Paused",
};

function phaseBadgeClass(phase: CapturePhase): string {
  if (phase === "recording") return "rounded bg-ok/15 px-2 py-0.5 text-xs font-semibold text-ok";
  if (phase === "paused") return "rounded bg-warn/15 px-2 py-0.5 text-xs font-semibold text-warn";
  if (TRANSITIONS.has(phase))
    return "rounded bg-warn/10 px-2 py-0.5 text-xs font-semibold text-warn";
  return "rounded bg-fg-dim/10 px-2 py-0.5 text-xs font-semibold text-fg-dim";
}

/** The polled snapshot agrees with an optimistic one on the three things the
 *  controls are driven by. */
function agrees(polled: CaptureProfileState, optimistic: CaptureProfileState): boolean {
  return (
    Boolean(polled.active) === Boolean(optimistic.active) &&
    polled.name === optimistic.name &&
    Boolean(polled.paused) === Boolean(optimistic.paused)
  );
}

export function CaptureRunner({
  token,
  status,
  channels,
}: {
  token: string;
  status: StatusResponse | null;
  /** Live values, so a running capture shows what it is recording. */
  channels?: Map<string, ChannelSnapshot>;
}) {
  const { catalog, error: catalogError } = useCaptureCatalog(token);
  const profiles = catalog?.profiles ?? [];
  const [selectedName, setSelectedName] = useState("default");
  // `null` until the operator types: until then the field offers the
  // calibration the daemon says is running.
  const [tuneRevisionEdit, setTuneRevisionEdit] = useState<string | null>(null);
  // `/status` polls every 5 s, so a capture that started instantly still read
  // as "not capturing" for up to five seconds — the whole reason start/stop
  // felt unresponsive. Every capture mutation already returns the new
  // snapshot; hold it here and let polling catch up.
  const [optimistic, setOptimistic] = useState<CaptureProfileState | null>(null);
  const [lastVerdict, setLastVerdict] = useState<CaptureQuality | null>(null);
  const { notice, busy, run } = useAsyncAction();

  const polled = status?.capture_profile ?? null;
  // Drop the optimistic snapshot as soon as polling agrees. Keeping it any
  // longer would mask a capture that the daemon ended on its own.
  if (optimistic && polled && agrees(polled, optimistic)) setOptimistic(null);
  const active = optimistic ?? polled;
  const capturing = Boolean(active?.active);
  const paused = active?.paused ?? false;
  const phase = capturePhase(busy, active);
  const transitioning = TRANSITIONS.has(phase);

  const tuneRevision = tuneRevisionEdit ?? polled?.running_tune_revision ?? "";
  const selectedProfile = profiles.find((profile) => profile.name === selectedName);
  // The daemon refuses these outright, and it is the only moment the value can
  // be recorded. Trimmed, because the daemon treats blank as absent too.
  const needsTuneRevision =
    Boolean(selectedProfile?.requires_tune_revision) && tuneRevision.trim() === "";
  const scheduler = status?.health?.obd_scheduler;
  const plan = capturing ? active : (selectedProfile ?? null);
  const resolvedPlan = capturing
    ? scheduler?.resolved_plan
    : plan
      ? catalog?.resolvedPlans[plan.name]
      : undefined;

  const togglePause = useCallback(() => {
    if (!token || !capturing) return;
    void run(
      paused ? "capture resume" : "capture pause",
      async () => {
        const result = await pauseProfileCapture(token, !paused);
        setOptimistic(result.capture_profile);
      },
      { okText: null },
    );
  }, [token, capturing, paused, run]);

  // The Space listener is installed once and calls the latest toggle.
  const togglePauseRef = useRef(togglePause);
  useEffect(() => {
    togglePauseRef.current = togglePause;
  }, [togglePause]);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.code !== "Space" || event.repeat) return;
      const target = event.target as HTMLElement | null;
      // Space belongs to whatever the operator is typing in or has focused;
      // only claim it when it would otherwise scroll the page.
      const tag = target?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || tag === "BUTTON") return;
      if (target?.isContentEditable) return;
      event.preventDefault();
      togglePauseRef.current();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const start = () =>
    run(
      "capture start",
      async () => {
        if (!selectedProfile) return;
        // Name-only on purpose: the daemon reloads the authoritative profile
        // from disk.
        const result = await startProfileCapture(
          token,
          selectedProfile.name,
          tuneRevision || undefined,
        );
        setOptimistic(result.capture_profile);
        setLastVerdict(null);
      },
      { okText: null },
    );

  const stop = () =>
    run(
      "capture stop",
      async () => {
        const result = await stopProfileCapture(token);
        setOptimistic(result.capture_profile);
        setLastVerdict(result.capture_quality ?? null);
      },
      { okText: null },
    );

  return (
    <Panel
      title="Logging"
      actions={<CollectionStatus active={active} />}
      className="flex flex-col gap-4"
    >
      <Notice notice={notice} />
      <Notice notice={busy ? { kind: "busy", text: `Working: ${busy}` } : null} />
      {catalogError && <Notice notice={{ kind: "error", text: catalogError }} />}
      {token && catalog && profiles.length === 0 && (
        <p className="text-sm text-bad">No capture profiles — the catalog failed to load.</p>
      )}

      <div className="rounded border border-border p-3" data-testid="capture-controls">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-semibold">Run a log</span>
          <span
            className={phaseBadgeClass(phase)}
            data-testid="capture-phase"
            role="status"
            aria-live="polite"
          >
            {phase !== "idle"
              ? PHASE_TEXT[phase]
              : active?.name === "default"
                ? "Default collection active"
                : "Collection status unavailable"}
          </span>
          {capturing && <span className="text-xs text-fg-dim">{active?.name}</span>}
        </div>

        <div className="mt-2 flex flex-wrap items-end gap-2">
          <ProfilePicker
            profiles={profiles}
            value={selectedName}
            onChange={setSelectedName}
            hint="choose the saved schema for this log"
            disabled={transitioning}
          />
          <Field
            label="Tune revision"
            hint={selectedProfile?.requires_tune_revision ? "required by this profile" : "optional"}
            className="max-[600px]:w-full"
          >
            <input
              className={`${inputClass} mt-1 w-48 max-[600px]:w-full`}
              value={tuneRevision}
              disabled={capturing || transitioning}
              onChange={(event) => setTuneRevisionEdit(event.target.value)}
              placeholder="ROM / tune revision note"
            />
          </Field>

          {!capturing ? (
            <button
              className={cn(buttonClass, RUN_BUTTON_PHONE)}
              type="button"
              disabled={!token || !selectedProfile || selectedProfile.name === "default" || !!busy}
              onClick={() => void start()}
            >
              {phase === "starting" ? "Starting…" : "Start"}
            </button>
          ) : (
            <>
              <button
                className={cn(buttonClass, RUN_BUTTON_PHONE)}
                type="button"
                disabled={!token || !!busy}
                onClick={togglePause}
              >
                {phase === "pausing"
                  ? "Pausing…"
                  : phase === "resuming"
                    ? "Resuming…"
                    : paused
                      ? "Resume"
                      : "Pause"}
                {!transitioning && (
                  <span className="ml-1 text-fg-dim max-[600px]:hidden">(space)</span>
                )}
              </button>
              <button
                className={cn(buttonClass, RUN_BUTTON_PHONE)}
                type="button"
                disabled={!token || !!busy}
                onClick={() => void stop()}
              >
                {phase === "stopping" ? "Stopping…" : "Stop"}
              </button>
            </>
          )}
        </div>
      </div>

      {lastVerdict && (
        <CaptureVerdict verdict={lastVerdict} onDismiss={() => setLastVerdict(null)} />
      )}

      {plan && plan.name !== "default" && (
        <CapturePlanSummary plan={plan} resolved={resolvedPlan} />
      )}

      {/* Advisory, never a gate (operator decision 2026-08-10). The capture
          runs either way; this only says what is lost by leaving it blank. */}
      {!capturing && needsTuneRevision && (
        <p className="text-xs text-fg-dim">
          No tune revision entered. {selectedProfile?.name} records fuelling or timing, which is
          only interpretable against the calibration in force — the capture will run, but it cannot
          be compared across a flash.
        </p>
      )}

      {capturing && active && (
        <RecordingReadout
          active={active}
          paused={paused}
          channels={channels}
          status={status}
          minRateHz={plan?.min_achieved_rate_hz ?? null}
        />
      )}
    </Panel>
  );
}

/** The contract verdict for the capture that just ended. Absent means no
 *  sidecar was written — unknown, never a pass. */
function CaptureVerdict({
  verdict,
  onDismiss,
}: {
  verdict: CaptureQuality;
  onDismiss: () => void;
}) {
  return (
    <div
      className={`rounded border p-3 text-sm ${
        verdict.verdict === "pass"
          ? "border-ok/50 text-ok"
          : verdict.verdict === "fail" || verdict.verdict === "degraded"
            ? "border-warn/60 text-warn"
            : "border-border text-fg-dim"
      }`}
      data-testid="capture-verdict"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-semibold">Capture ended — contract {verdict.verdict}</span>
        <span className="text-xs text-fg-dim">scope: {verdict.scope}</span>
        {verdict.tuples?.complete != null && (
          <span className="text-xs">
            {verdict.tuples.complete} complete tuples
            {verdict.tuples.attempted != null ? ` of ${verdict.tuples.attempted} rounds` : ""}
          </span>
        )}
        {verdict.tuple_rate_bound_hz != null && (
          <span className="text-xs">
            required tuple ≤ {verdict.tuple_rate_bound_hz.toFixed(2)} Hz (bound)
          </span>
        )}
        <button className="ml-auto text-xs underline" type="button" onClick={onDismiss}>
          dismiss
        </button>
      </div>
      {verdict.failures.map((failure) => (
        <p key={failure.clause} className="mt-1 text-xs">
          <span className="font-semibold">{failure.clause}</span>
          {failure.channels?.length ? `: ${failure.channels.join(", ")}` : ""}
          {failure.detail ? ` — ${failure.detail}` : ""}
        </p>
      ))}
    </div>
  );
}

interface LiveRow {
  channel: string;
  value: string;
  unit: string;
}

/** Latest value of each channel the running capture selected; a sample from
 *  before this capture started reads as "Waiting for sample". */
function liveValues(
  active: CaptureProfileState,
  channels: Map<string, ChannelSnapshot> | undefined,
): LiveRow[] {
  const wanted = [
    ...(active.mode01_enabled
      ? Object.keys(active.mode01).map((name) => ({ label: name, key: `obd:${name}` }))
      : []),
    ...(active.mode22_enabled
      ? active.mode22.map((name) => ({ label: `did:${name}`, key: `did:${name}` }))
      : []),
    ...(active.mode22_enabled
      ? (active.memory ?? []).map((name) => ({
          label: `mem:${name}`,
          key: streamKey(`mem:${name}`),
        }))
      : []),
  ];
  const startedAt = active.started_at ? Date.parse(active.started_at) : NaN;
  return wanted.map(({ label, key }) => {
    const snapshot = channels?.get(key) ?? channels?.get(label);
    const sampleAt = snapshot?.latest.timestamp ? Date.parse(snapshot.latest.timestamp) : NaN;
    const current =
      snapshot &&
      (!Number.isFinite(startedAt) || !Number.isFinite(sampleAt) || sampleAt >= startedAt);
    if (!current) return { channel: label, value: "Waiting for sample", unit: "" };
    const value = snapshot.latest.value;
    return {
      channel: label,
      value: typeof value === "number" ? formatReading(value) : String(value ?? "—"),
      unit: snapshot.latest.unit ?? "",
    };
  });
}

/** Seconds since `startedAt`, ticking once a second. */
function useCaptureAge(startedAt: string | null): number {
  const now = useNow();
  return startedAt ? Math.max(0, (now - Date.parse(startedAt)) / 1000) : 0;
}

function RecordingReadout({
  active,
  paused,
  channels,
  status,
  minRateHz,
}: {
  active: CaptureProfileState;
  paused: boolean;
  channels?: Map<string, ChannelSnapshot>;
  status: StatusResponse | null;
  minRateHz: number | null;
}) {
  const captureAgeSeconds = useCaptureAge(active.started_at);
  const rows = liveValues(active, channels);
  const scheduler = status?.health?.obd_scheduler;
  const achievedRates = Object.entries(scheduler?.achieved_hz_capture ?? {}).sort(([a], [b]) =>
    a.localeCompare(b),
  );
  const requestRows = (scheduler?.requests ?? [])
    .filter((row) => row.capture_selected)
    .map((row) => ({
      ...row,
      displayChannel: row.profile_channel || row.channel,
      achievedHz: scheduler?.achieved_hz_capture?.[row.profile_channel || row.channel] ?? 0,
    }))
    .sort((a, b) => a.displayChannel.localeCompare(b.displayChannel));
  const maxGaps = scheduler?.capture_max_gap_seconds ?? {};
  const didWindow = scheduler?.did_profile;

  return (
    <div className="rounded border border-border p-2">
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-sm">
        <span className={paused ? "font-semibold text-warn" : "font-semibold text-ok"}>
          {paused ? "PAUSED" : "RECORDING"}
        </span>
        <span className="font-num tabular-nums">
          {(active.rows_written ?? 0).toLocaleString()} rows
        </span>
        <span className="text-fg-dim">{active.session_dir ?? "rotating storage"}</span>
        {/* Rows land on disk on the writer's own timer, so there is nothing
            to lose by pausing or navigating away. */}
        <span className="text-[0.72rem] text-fg-dim">saved continuously</span>
      </div>
      {rows.length > 0 && (
        <table className="mt-2 w-full font-num text-[0.78rem] tabular-nums">
          <tbody>
            {rows.map((row) => (
              <tr key={row.channel} className="border-b border-border/40">
                <td className="py-0.5 pr-3 text-fg-dim">{row.channel}</td>
                <td className="py-0.5 pr-2 text-right">{row.value}</td>
                <td className="py-0.5 text-left text-fg-dim">{row.unit}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {(achievedRates.length > 0 || didWindow) && (
        <div className="mt-3 border-t border-border/60 pt-2 text-xs">
          {didWindow && (
            <p className={didWindow.fast_exit_failures > 0 ? "text-warn" : "text-fg-dim"}>
              DID session: {didWindow.phase} · fast exits {didWindow.fast_exits}
              {` · failures ${didWindow.fast_exit_failures}`}
              {!didWindow.fast_exit_supported &&
                (didWindow.fast_exit_retry_in_seconds
                  ? ` · Mode 01 degraded${
                      didWindow.last_exit_nrc_name ? ` (${didWindow.last_exit_nrc_name})` : ""
                    }, retrying in ${Math.ceil(didWindow.fast_exit_retry_in_seconds)}s`
                  : " · fast exit unavailable")}
            </p>
          )}
          {requestRows.length > 0 ? (
            <TableScroll className="mt-1">
              <table className="w-full min-w-[30rem] font-num text-[0.72rem] tabular-nums">
                <thead>
                  <tr className="text-fg-dim">
                    <th className="text-left">Channel</th>
                    <th>kind</th>
                    <th>requested</th>
                    <th>achieved</th>
                    <th>sent</th>
                    <th>responses</th>
                    <th>timeouts</th>
                    <th>NRCs</th>
                  </tr>
                </thead>
                <tbody>
                  {requestRows.map((row) => {
                    const neverSent = captureAgeSeconds >= 10 && (row.requests_sent ?? 0) === 0;
                    const short = minRateHz != null && row.achievedHz < minRateHz;
                    return (
                      <tr
                        key={`${row.kind}:${row.channel}`}
                        className={`border-b border-border/40 ${neverSent ? "text-bad" : short ? "text-warn" : ""}`}
                      >
                        <td>{row.displayChannel}</td>
                        <td className="text-center">{row.kind}</td>
                        <td className="text-right">{effectiveTargetHz(row)?.toFixed(2) ?? "—"}</td>
                        <td className="text-right">{row.achievedHz.toFixed(2)}</td>
                        <td className="text-right">{row.requests_sent ?? 0}</td>
                        <td className="text-right">{row.responses ?? 0}</td>
                        <td className="text-right">{row.timeouts ?? 0}</td>
                        <td className="text-right">{row.negative_responses ?? 0}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </TableScroll>
          ) : (
            achievedRates.length > 0 && (
              <table className="mt-1 w-full font-num tabular-nums">
                <thead>
                  <tr className="text-fg-dim">
                    <th className="text-left">Captured channel</th>
                    <th className="text-right">Hz</th>
                    <th className="text-right">max gap</th>
                  </tr>
                </thead>
                <tbody>
                  {achievedRates.map(([channel, hz]) => (
                    <tr key={channel} className="border-b border-border/40">
                      <td>{channel}</td>
                      <td className="text-right">{hz.toFixed(2)}</td>
                      <td className="text-right">
                        {maxGaps[channel] == null ? "—" : `${maxGaps[channel].toFixed(2)} s`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          )}
        </div>
      )}
    </div>
  );
}
