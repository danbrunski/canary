import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  deleteCaptureProfile,
  getCaptureProfiles,
  getDids,
  getObdCoverage,
  pauseProfileCapture,
  saveCaptureProfile,
  startProfileCapture,
  stopProfileCapture,
} from "../api/http";
import { Notice } from "../components/Notice";
import { useAsyncAction } from "../hooks/useAsyncAction";
import {
  buttonClass,
  dangerButtonClass,
  inputClass,
  panelClass,
  panelHeaderClass,
  panelTitleClass,
} from "../styles";
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import type {
  CaptureProfile,
  CaptureProfileState,
  CaptureProfileWarning,
  CaptureQuality,
  ResolvedPollingPlan,
  StatusResponse,
} from "../types";

/** Numeric settings are 2–4 digits. Sizing them to the grid column made a
 *  bus-budget box span half the page and read as a free-text field. */
const numFieldClass = `${inputClass} mt-1 w-24 text-right`;

/** A labelled control sized to its content, with the units and the
 *  blank-means-what note demoted out of the label so the label stays short. */
function Field({
  label, hint, className = "", children,
}: {
  label: string;
  hint?: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <label className={`text-sm text-fg-dim ${className}`}>
      <span className="block">{label}</span>
      {children}
      {hint && <span className="mt-0.5 block text-[0.68rem] text-fg-dim/80">{hint}</span>}
    </label>
  );
}

/** `2026-07-31-1830` — a lowercase slug the backend name validator accepts,
 *  local time so it matches when the operator remembers running it. */
function timestampSlug(now = new Date()): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  return [
    now.getFullYear(),
    pad(now.getMonth() + 1),
    pad(now.getDate()),
    `${pad(now.getHours())}${pad(now.getMinutes())}`,
  ].join("-");
}

const blankProfile = (): CaptureProfile => {
  const name = timestampSlug();
  return {
    name,
    description: "",
    experimental: false,
    mode01: {},
    mode22: [],
    memory: [],
    mode22_rate_weights: {},
    mode01_enabled: true,
    mode22_enabled: true,
    did_interval_seconds: null,
    round_timestamps: true,
    target_rate_hz: null,
    did_continuous: false,
    bus_budget_override: null,
    priority_channels: null,
    expected_total_demand_hz: null,
    capacity_evidence: "",
    required_tuple: [],
    min_achieved_rate_hz: null,
    requires_tune_revision: false,
    broadcast_decode: true,
    corpus_eligible: false,
    behavior_model_recipes: [],
    storage_prefix: `_capture-${name}`,
  };
};

export function CaptureProfilesPanel({
  token,
  status,
  channels,
  authoring = true,
  showRunner = true,
}: {
  token: string;
  status: StatusResponse | null;
  /** Live values, so a running capture shows what it is recording. */
  channels?: Map<string, ChannelSnapshot>;
  /** Logging reuses the runner/readout without exposing profile editing. */
  authoring?: boolean;
  /** System edits profiles; Logging owns starting and stopping captures. */
  showRunner?: boolean;
}) {
  const [profiles, setProfiles] = useState<CaptureProfile[]>([]);
  const [resolvedPlans, setResolvedPlans] = useState<Record<string, ResolvedPollingPlan>>({});
  const [selectedName, setSelectedName] = useState("default");
  const [draft, setDraft] = useState<CaptureProfile>(blankProfile());
  const [originalName, setOriginalName] = useState<string | undefined>();
  const [pids, setPids] = useState<string[]>([]);
  const [dids, setDids] = useState<string[]>([]);
  const [memoryChannels, setMemoryChannels] = useState<string[]>([]);
  const [warnings, setWarnings] = useState<CaptureProfileWarning[]>([]);
  const [tuneRevision, setTuneRevision] = useState("");
  const [showWeights, setShowWeights] = useState(false);
  // `/status` polls every 5 s, so a capture that started instantly still read
  // as "not capturing" for up to five seconds — the whole reason start/stop
  // felt unresponsive. Every capture mutation already returns the new
  // snapshot; hold it here and let polling catch up.
  const [optimistic, setOptimistic] = useState<CaptureProfileState | null>(null);
  const [lastVerdict, setLastVerdict] = useState<CaptureQuality | null>(null);
  const { notice, setNotice, busy, run } = useAsyncAction();

  const refresh = useCallback(async () => {
    if (!token) return;
    const [catalog, obd, didMap] = await Promise.all([
      getCaptureProfiles(token),
      getObdCoverage(),
      getDids(),
    ]);
    setProfiles(catalog.profiles);
    setResolvedPlans(catalog.resolved_plans ?? {});
    setPids(
      obd.rows
        .filter((row) => row.channel && row.status === "decoded")
        .map((row) => row.channel as string)
        .sort(),
    );
    setDids(didMap.dids.map((entry) => entry.name).sort());
    setMemoryChannels((didMap.memory ?? []).map((entry) => entry.name).sort());
  }, [token]);

  useEffect(() => {
    refresh().catch((error: unknown) =>
      setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) }),
    );
  }, [refresh, setNotice]);

  useEffect(() => {
    const selected = profiles.find((profile) => profile.name === selectedName);
    if (selected) {
      setDraft(structuredClone(selected));
      setOriginalName(selected.name);
      setWarnings([]);
    }
  }, [profiles, selectedName]);

  const polled = status?.capture_profile ?? null;
  const active = optimistic ?? polled;
  const paused = active?.paused ?? false;

  useEffect(() => {
    const running = polled?.running_tune_revision;
    if (!active?.active && !tuneRevision && running) setTuneRevision(running);
  }, [active?.active, polled?.running_tune_revision, tuneRevision]);

  // Drop the optimistic snapshot as soon as polling agrees on the three things
  // the controls are driven by. Keeping it any longer would mask a capture that
  // the daemon ended on its own.
  useEffect(() => {
    if (!optimistic) return;
    if (!polled) return;
    if (
      Boolean(polled.active) === Boolean(optimistic.active)
      && polled.name === optimistic.name
      && Boolean(polled.paused) === Boolean(optimistic.paused)
    ) {
      setOptimistic(null);
    }
  }, [polled, optimistic]);

  /** What the controls are doing right now. An in-flight transition is a
   *  distinct state from the settled ones: "Starting…" is the answer to the
   *  complaint that nothing happens when Start is pressed. */
  const phase: "idle" | "starting" | "stopping" | "pausing" | "resuming" | "recording" | "paused" =
    busy === "capture start" ? "starting"
    : busy === "capture stop" ? "stopping"
    : busy === "capture pause" ? "pausing"
    : busy === "capture resume" ? "resuming"
    : active?.active ? (paused ? "paused" : "recording")
    : "idle";
  const transitioning = phase === "starting" || phase === "stopping"
    || phase === "pausing" || phase === "resuming";
  const selectedProfile = profiles.find((profile) => profile.name === selectedName);
  // Starting is intentionally name-only: the daemon reloads the authoritative
  // profile from disk.  Never present an edited draft as the plan that request
  // will launch, and require a save before the button becomes actionable.
  const plan = showRunner && active?.active ? active : (selectedProfile ?? draft);
  const hasUnsavedChanges = selectedProfile == null
    || JSON.stringify(draft) !== JSON.stringify(selectedProfile);
  // The daemon refuses these outright, and it is the only moment the value can
  // be recorded. Gating here turns a round-trip rejection into a control that
  // was never armable — the operator finds out before pressing Start, not
  // after. Trimmed, because the daemon treats blank as absent too.
  const needsTuneRevision = Boolean(selectedProfile?.requires_tune_revision)
    && tuneRevision.trim() === "";
  const scheduler = status?.health?.obd_scheduler;
  const achievedRates = Object.entries(scheduler?.achieved_hz_capture ?? {}).sort(
    ([a], [b]) => a.localeCompare(b),
  );
  const requestRows = (scheduler?.requests ?? [])
    .filter((row) => row.capture_selected)
    .map((row) => ({
      ...row,
      displayChannel: row.profile_channel || row.channel,
      achievedHz:
        scheduler?.achieved_hz_capture?.[row.profile_channel || row.channel] ?? 0,
    }))
    .sort((a, b) => a.displayChannel.localeCompare(b.displayChannel));
  const captureAgeSeconds = active?.started_at
    ? Math.max(0, (Date.now() - Date.parse(active.started_at)) / 1000)
    : 0;
  const maxGaps = scheduler?.capture_max_gap_seconds ?? {};
  const didWindow = scheduler?.did_profile;
  const mode01Count = plan.mode01_enabled ? Object.keys(plan.mode01).length : 0;
  const mode22Count = plan.mode22_enabled ? plan.mode22.length : 0;
  const memoryCount = plan.mode22_enabled ? (plan.memory?.length ?? 0) : 0;
  const sessionPolicy = mode22Count + memoryCount === 0
    ? "Mode 01 only"
    : plan.did_continuous
      ? "Mode 22 session held open"
      : mode01Count > 0
        ? "Mode 01 / Mode 22 alternating"
        : "Mode 22 windowed";
  const resolvedPlan = active?.active
    ? scheduler?.resolved_plan
    : resolvedPlans[plan.name];

  const togglePause = useCallback(() => {
    if (!showRunner || !token || !active?.active) return;
    void run(paused ? "capture resume" : "capture pause", async () => {
      const result = await pauseProfileCapture(token, !paused);
      setOptimistic(result.capture_profile);
    }, { okText: null });
  }, [showRunner, token, active?.active, paused, run]);

  const togglePauseRef = useRef(togglePause);
  togglePauseRef.current = togglePause;

  useEffect(() => {
    if (!showRunner) return;
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
  }, [showRunner]);
  const mode01Set = useMemo(
    () => new Set("AUTO" in draft.mode01 ? pids : Object.keys(draft.mode01)),
    [draft.mode01, pids],
  );
  const mode22Set = useMemo(() => new Set(draft.mode22), [draft.mode22]);
  const memorySet = useMemo(() => new Set(draft.memory ?? []), [draft.memory]);

  /** Latest value of each channel the *running* capture selected. */
  const liveValues = useMemo(() => {
    if (!showRunner || !active?.active) return [];
    const wanted = [
      ...(active.mode01_enabled
        ? Object.keys(active.mode01).map((name) => ({ label: name, key: `obd:${name}` })) : []),
      ...(active.mode22_enabled
        ? active.mode22.map((name) => ({ label: `did:${name}`, key: `did:${name}` })) : []),
      ...(active.mode22_enabled
        ? (active.memory ?? []).map((name) => ({ label: `mem:${name}`, key: `obd:mem:${name}` })) : []),
    ];
    const startedAt = active.started_at ? Date.parse(active.started_at) : NaN;
    return wanted.map(({ label, key }) => {
      const snapshot = channels?.get(key) ?? channels?.get(label);
      const sampleAt = snapshot?.latest.timestamp ? Date.parse(snapshot.latest.timestamp) : NaN;
      const current = snapshot && (
        !Number.isFinite(startedAt) || !Number.isFinite(sampleAt) || sampleAt >= startedAt
      );
      const value = current ? snapshot.latest.value : null;
      return {
        channel: label,
        value: current
          ? (typeof value === "number" ? value.toFixed(2) : String(value ?? "—"))
          : "Waiting for sample",
        unit: current ? (snapshot.latest.unit ?? "") : "",
      };
    });
  }, [showRunner, active?.active, active?.started_at, active?.mode01_enabled, active?.mode01,
    active?.mode22_enabled, active?.mode22, active?.memory, channels]);

  const togglePid = (channel: string) => {
    const next = "AUTO" in draft.mode01
      ? Object.fromEntries(pids.map((name) => [name, 1]))
      : { ...draft.mode01 };
    if (channel in next) delete next[channel];
    else next[channel] = 1;
    setDraft({ ...draft, mode01: next });
  };
  const toggleDid = (name: string) => {
    const selected = mode22Set.has(name);
    const weights = { ...draft.mode22_rate_weights };
    if (selected) delete weights[name];
    setDraft({
      ...draft,
      mode22: selected
        ? draft.mode22.filter((item) => item !== name)
        : [...draft.mode22, name],
      mode22_rate_weights: weights,
    });
  };
  const toggleMemory = (name: string) => {
    setDraft({
      ...draft,
      memory: memorySet.has(name)
        ? (draft.memory ?? []).filter((item) => item !== name)
        : [...(draft.memory ?? []), name],
    });
  };

  const save = (saveAs: boolean) =>
    run(saveAs ? "capture profile save-as" : "capture profile save", async () => {
      const result = await saveCaptureProfile(token, draft, saveAs ? undefined : originalName);
      setWarnings(result.warnings);
      setSelectedName(result.profile.name);
    }, { then: refresh });

  const profilePicker = (
    <Field
      label="Profile"
      hint={authoring && showRunner ? "picks what runs — and what the editor below shows"
        : authoring ? "choose a profile to edit" : "choose the saved schema for this log"}
    >
      <select
        className={`${inputClass} mt-1 w-56`}
        value={selectedName}
        disabled={transitioning}
        onChange={(event) => setSelectedName(event.target.value)}
      >
        {profiles.map((profile) => <option key={profile.name}>{profile.name}</option>)}
      </select>
    </Field>
  );

  return (
    <section className={`${panelClass} flex flex-col gap-4`}>
      <header className={panelHeaderClass}>
        <h2 className={panelTitleClass}>{authoring ? "Capture profiles" : "Logging"}</h2>
        <span className={active?.active ? "text-warn" : "text-fg-dim"}>
          {active?.active ? `active: ${active.name}`
            : active?.name === "default" ? "default collection" : "status unavailable"}
        </span>
      </header>
      <Notice notice={notice} />
      <Notice notice={busy ? { kind: "busy", text: `Working: ${busy}` } : null} />
      {token && profiles.length === 0 && !busy && (
        <p className="text-sm text-bad">No capture profiles — the catalog failed to load.</p>
      )}

      {authoring && !showRunner && (
        <>
          <p className="text-sm text-fg-dim">
            Define the default collection and temporary logging schemas here. Start a log from Engine Tuning → Logging.
          </p>
          <div className="flex flex-wrap gap-2">{profilePicker}</div>
        </>
      )}

      {/* Running a capture and authoring a profile are different jobs that
          shared one button row, which is what made "New" ambiguous. They are
          now separate blocks with the relationship stated once. */}
      {showRunner && (
      <div className="rounded border border-border p-3" data-testid="capture-controls">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-semibold">Run a log</span>
          <span
            className={
              phase === "recording" ? "rounded bg-ok/15 px-2 py-0.5 text-xs font-semibold text-ok"
              : phase === "paused" ? "rounded bg-warn/15 px-2 py-0.5 text-xs font-semibold text-warn"
              : transitioning ? "rounded bg-warn/10 px-2 py-0.5 text-xs font-semibold text-warn"
              : "rounded bg-fg-dim/10 px-2 py-0.5 text-xs font-semibold text-fg-dim"
            }
            data-testid="capture-phase"
            role="status"
            aria-live="polite"
          >
            {phase === "starting" ? "Starting…"
              : phase === "stopping" ? "Stopping…"
              : phase === "pausing" ? "Pausing…"
              : phase === "resuming" ? "Resuming…"
              : phase === "recording" ? "Recording"
              : phase === "paused" ? "Paused"
              : active?.name === "default" ? "Default collection active" : "Collection status unavailable"}
          </span>
          {active?.active && <span className="text-xs text-fg-dim">{active.name}</span>}
        </div>

        <div className="mt-2 flex flex-wrap items-end gap-2">
          {profilePicker}
          <Field
            label="Tune revision"
            hint={selectedProfile?.requires_tune_revision ? "required by this profile" : "optional"}
          >
            <input
              className={`${inputClass} mt-1 w-48`}
              value={tuneRevision}
              disabled={active?.active || transitioning}
              onChange={(event) => setTuneRevision(event.target.value)}
              placeholder="ROM / tune revision note"
            />
          </Field>

          {!active?.active ? (
            <button
              className={buttonClass}
              type="button"
              title={hasUnsavedChanges ? "Save profile changes before starting" : undefined}
              disabled={!token || selectedProfile?.name === "default" || hasUnsavedChanges || !!busy}
              onClick={() => run("capture start", async () => {
                const result = await startProfileCapture(
                  token, selectedProfile!.name, tuneRevision || undefined,
                );
                setOptimistic(result.capture_profile);
                setLastVerdict(null);
              }, { okText: null })}
            >{phase === "starting" ? "Starting…" : "Start"}</button>
          ) : (
            <>
              <button className={buttonClass} type="button" disabled={!token || !!busy} onClick={togglePause}>
                {phase === "pausing" ? "Pausing…"
                  : phase === "resuming" ? "Resuming…"
                  : paused ? "Resume" : "Pause"}
                {!transitioning && <span className="ml-1 text-fg-dim">(space)</span>}
              </button>
              <button className={buttonClass} type="button" disabled={!token || !!busy} onClick={() => run("capture stop", async () => {
                const result = await stopProfileCapture(token);
                setOptimistic(result.capture_profile);
                setLastVerdict(result.capture_quality ?? null);
              }, { okText: null })}>{phase === "stopping" ? "Stopping…" : "Stop"}</button>
            </>
          )}
        </div>
      </div>
      )}

      {/* The contract verdict for the capture that just ended. Absent means no
          sidecar was written — unknown, never a pass. */}
      {showRunner && lastVerdict && (
        <div
          className={`rounded border p-3 text-sm ${
            lastVerdict.verdict === "pass"
              ? "border-ok/50 text-ok"
              : lastVerdict.verdict === "fail" || lastVerdict.verdict === "degraded"
                ? "border-warn/60 text-warn"
                : "border-border text-fg-dim"
          }`}
          data-testid="capture-verdict"
        >
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-semibold">
              Capture ended — contract {lastVerdict.verdict}
            </span>
            <span className="text-xs text-fg-dim">scope: {lastVerdict.scope}</span>
            {lastVerdict.tuples?.complete != null && (
              <span className="text-xs">
                {lastVerdict.tuples.complete} complete tuples
                {lastVerdict.tuples.attempted != null ? ` of ${lastVerdict.tuples.attempted} rounds` : ""}
              </span>
            )}
            {lastVerdict.tuple_rate_bound_hz != null && (
              <span className="text-xs">
                required tuple ≤ {lastVerdict.tuple_rate_bound_hz.toFixed(2)} Hz (bound)
              </span>
            )}
            <button
              className="ml-auto text-xs underline"
              type="button"
              onClick={() => setLastVerdict(null)}
            >dismiss</button>
          </div>
          {lastVerdict.failures.map((failure) => (
            <p key={failure.clause} className="mt-1 text-xs">
              <span className="font-semibold">{failure.clause}</span>
              {failure.channels?.length ? `: ${failure.channels.join(", ")}` : ""}
              {failure.detail ? ` — ${failure.detail}` : ""}
            </p>
          ))}
        </div>
      )}

      {plan.name !== "default" && (
        <div className="rounded border border-border p-3 text-sm" data-testid="capture-plan">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-semibold">Capture plan: {plan.name}</span>
            {plan.experimental && (
              <span className="rounded border border-warn/60 bg-warn/10 px-1.5 py-0.5 text-[0.68rem] font-semibold uppercase text-warn">
                Experimental
              </span>
            )}
          </div>
          {plan.description && <p className="mt-1 text-fg-dim">{plan.description}</p>}
          {plan.notes && <p className="mt-1 text-xs text-fg-dim">{plan.notes}</p>}
          <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-fg-dim">
            <span>{mode01Count} Mode 01 channels</span>
            <span>{mode22Count} Mode 22 DIDs</span>
            <span>{memoryCount} memory channels</span>
            <span>{plan.target_rate_hz == null ? "relative cadence" : `${plan.target_rate_hz} Hz/channel`}</span>
            <span>{sessionPolicy}</span>
            {plan.did_interval_seconds != null && <span>DID window every {plan.did_interval_seconds}s</span>}
            {plan.bus_budget_override != null && <span>{plan.bus_budget_override} requests/s budget</span>}
            {plan.expected_total_demand_hz != null && <span>{plan.expected_total_demand_hz} expected channel reads/s</span>}
            <span>{plan.corpus_eligible ? "training eligible" : "training corpus excluded"}</span>
            {plan.min_achieved_rate_hz != null && <span>{plan.min_achieved_rate_hz} Hz floor enforced</span>}
            {plan.requires_tune_revision && <span>tune revision required</span>}
            {plan.behavior_model_recipes.length > 0 && (
              <span>{plan.behavior_model_recipes.length} behavior-model recipes allowed</span>
            )}
          </div>
          {mode01Count > 0 && <p className="mt-2 text-xs text-fg-dim">Mode 01: {Object.keys(plan.mode01).join(", ")}</p>}
          {mode22Count > 0 && <p className="mt-2 text-xs text-fg-dim">Mode 22: {plan.mode22.join(", ")}</p>}
          {memoryCount > 0 && (
            <p className="mt-2 text-xs text-fg-dim">
              RAM reads (service 0x23): {(plan.memory ?? []).join(", ")}
            </p>
          )}
          {resolvedPlan && (
            <div className={`mt-2 rounded border p-2 text-xs ${resolvedPlan.best_case_feasible ? "border-border" : "border-danger/60 text-danger"}`}>
              <span>{resolvedPlan.channel_reads_per_second} channel reads/s</span>
              {" · "}<span>{resolvedPlan.diagnostic_requests_per_second.best_case_batched} requests/s batched</span>
              {" · "}<span>{resolvedPlan.diagnostic_requests_per_second.single_request_fallback} requests/s fallback</span>
              {" · "}<span>{resolvedPlan.raw_frames_per_second.best_case_batched} raw frames/s{resolvedPlan.raw_frames_per_second.estimate_complete ? "" : " (lower bound; unknown widths)"}</span>
              {" · "}<span>{resolvedPlan.decoded_rows_per_second} decoded rows/s</span>
              {resolvedPlan.complete_tuples_per_second != null && <>{" · "}<span>{resolvedPlan.complete_tuples_per_second} complete tuples/s requested</span></>}
              {!resolvedPlan.priority_feasible && <p className="mt-1 font-semibold">Priority demand alone exceeds the bus budget.</p>}
              {!resolvedPlan.best_case_feasible && <p className="mt-1 font-semibold">Plan is infeasible even with configured batching.</p>}
              {resolvedPlan.best_case_feasible && !resolvedPlan.fallback_feasible && <p className="mt-1 text-warn">Single-request fallback exceeds the budget; quality will report rate shortfall if batching falls back.</p>}
            </div>
          )}
          {plan.required_tuple.length > 0 && (
            /* The joint requirement, shown because it is the thing per-channel
               rates cannot express: these must land in the same poll round or
               the capture yields nothing regardless of how healthy each looks. */
            <p className="mt-2 text-xs text-fg-dim">
              Required together: {plan.required_tuple.join(", ")}
            </p>
          )}
          {plan.capacity_evidence && <p className="mt-2 text-xs text-fg-dim">{plan.capacity_evidence}</p>}
        </div>
      )}

      {showRunner && !active?.active && hasUnsavedChanges && draft.name !== "default" && (
        <p className="text-xs text-warn">Save this profile before starting it.</p>
      )}
      {/* Advisory, never a gate (operator decision 2026-08-10). The capture
          runs either way; this only says what is lost by leaving it blank. */}
      {showRunner && !active?.active && needsTuneRevision && (
        <p className="text-xs text-fg-dim">
          No tune revision entered. {selectedProfile?.name} records fuelling or timing, which is
          only interpretable against the calibration in force — the capture will run, but it
          cannot be compared across a flash.
        </p>
      )}

      {showRunner && active?.active && (
        <div className="rounded border border-border p-2">
          <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-sm">
            <span className={paused ? "font-semibold text-warn" : "font-semibold text-ok"}>
              {paused ? "PAUSED" : "RECORDING"}
            </span>
            <span className="font-num tabular-nums">
              {(active.rows_written ?? 0).toLocaleString()} rows
            </span>
            <span className="text-fg-dim">{active.session_dir ?? "rotating storage"}</span>
            {/* Rows land on disk on the writer's own timer, so there is
                nothing to lose by pausing or navigating away. */}
            <span className="text-[0.72rem] text-fg-dim">saved continuously</span>
          </div>
          {liveValues.length > 0 && (
            <table className="mt-2 w-full font-num text-[0.78rem] tabular-nums">
              <tbody>
                {liveValues.map((row) => (
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
                <table className="mt-1 w-full font-num text-[0.72rem] tabular-nums">
                  <thead><tr className="text-fg-dim"><th className="text-left">Channel</th><th>kind</th><th>requested</th><th>achieved</th><th>sent</th><th>responses</th><th>timeouts</th><th>NRCs</th></tr></thead>
                  <tbody>
                    {requestRows.map((row) => {
                      const neverSent = captureAgeSeconds >= 10 && (row.requests_sent ?? 0) === 0;
                      const short = plan.min_achieved_rate_hz != null
                        && row.achievedHz < plan.min_achieved_rate_hz;
                      return (
                        <tr key={`${row.kind}:${row.channel}`} className={`border-b border-border/40 ${neverSent ? "text-bad" : short ? "text-warn" : ""}`}>
                          <td>{row.displayChannel}</td><td className="text-center">{row.kind}</td>
                          <td className="text-right">{row.configured_rate_hz.toFixed(2)}</td>
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
              ) : achievedRates.length > 0 && (
                <table className="mt-1 w-full font-num tabular-nums">
                  <thead><tr className="text-fg-dim"><th className="text-left">Captured channel</th><th className="text-right">Hz</th><th className="text-right">max gap</th></tr></thead>
                  <tbody>
                    {achievedRates.map(([channel, hz]) => (
                      <tr key={channel} className="border-b border-border/40">
                        <td>{channel}</td>
                        <td className="text-right">{hz.toFixed(2)}</td>
                        <td className="text-right">{maxGaps[channel] == null ? "—" : `${maxGaps[channel].toFixed(2)} s`}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </div>
      )}

      {authoring && <>
      <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
        <span className="text-sm font-semibold">Edit profile</span>
        <span className="text-xs text-fg-dim">
          {originalName
            ? `editing “${originalName}” — Save overwrites it, Save as makes a copy`
            : "new, unsaved profile — Save to add it to the list"}
        </span>
        <button
          className={`${buttonClass} ml-auto`}
          type="button"
          title="Only needed to create an additional profile. To run or change an existing one, pick it from Profile above."
          onClick={() => {
            const next = blankProfile();
            setDraft(next);
            setOriginalName(undefined);
            setSelectedName("");
          }}
        >New profile</button>
      </div>

      <div className="grid grid-cols-2 gap-3 max-[900px]:grid-cols-1">
        <label className="text-sm text-fg-dim">Name
          <input className={`${inputClass} mt-1 w-full`} value={draft.name} disabled={draft.name === "default"} onChange={(event) => {
            const name = event.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "");
            // Carry an auto-derived prefix along with the rename, so captures
            // don't land under the name the profile used to have. A prefix the
            // operator typed themselves is left alone.
            const derived = draft.storage_prefix === `_capture-${draft.name}`;
            setDraft({
              ...draft,
              name,
              storage_prefix: derived && !draft.corpus_eligible ? `_capture-${name}` : draft.storage_prefix,
            });
          }} />
        </label>
        <label className="text-sm text-fg-dim">Description
          <input className={`${inputClass} mt-1 w-full`} value={draft.description} onChange={(event) => setDraft({ ...draft, description: event.target.value })} />
        </label>
        {/* Numeric settings are a handful of digits — sized to fit, not to the
            column, so they stop reading as free-text fields. */}
        <div className="col-span-2 flex flex-wrap gap-x-4 gap-y-2 max-[900px]:col-span-1">
          <Field label="Bus budget" hint="req/s · blank = default">
            <input aria-label="Bus budget" className={numFieldClass} type="number" min="2" max="200" value={draft.bus_budget_override ?? ""} onChange={(event) => setDraft({ ...draft, bus_budget_override: event.target.value ? Number(event.target.value) : null })} />
          </Field>
          <Field label="Target rate" hint="Hz/channel · blank = relative">
            <input aria-label="Target rate" className={numFieldClass} type="number" min="0.1" step="0.1" value={draft.target_rate_hz ?? ""} onChange={(event) => setDraft({ ...draft, target_rate_hz: event.target.value ? Number(event.target.value) : null })} />
          </Field>
          <Field label="Expected demand" hint="channel reads/s">
            <input aria-label="Expected demand" className={numFieldClass} type="number" min="0.1" step="0.1" value={draft.expected_total_demand_hz ?? ""} onChange={(event) => setDraft({ ...draft, expected_total_demand_hz: event.target.value ? Number(event.target.value) : null })} />
          </Field>
          <Field label="Rate floor" hint="Hz · enforced by the contract">
            <input aria-label="Rate floor" className={numFieldClass} type="number" min="0.1" step="0.1" value={draft.min_achieved_rate_hz ?? ""} onChange={(event) => setDraft({ ...draft, min_achieved_rate_hz: event.target.value ? Number(event.target.value) : null })} />
          </Field>
          <Field label="Mode 22 window" hint="seconds · blank = daemon default">
            <input aria-label="Mode 22 window" className={numFieldClass} type="number" min="0.1" step="0.1" disabled={!draft.mode22_enabled} value={draft.did_interval_seconds ?? ""} onChange={(event) => setDraft({ ...draft, did_interval_seconds: event.target.value ? Number(event.target.value) : null })} />
          </Field>
          <Field label="Storage prefix" hint="must be _capture-<slug>" className="grow">
            <input aria-label="Storage prefix" className={`${inputClass} mt-1 w-64 max-w-full`} value={draft.storage_prefix} disabled={draft.name === "default" || draft.corpus_eligible} onChange={(event) => setDraft({ ...draft, storage_prefix: event.target.value })} />
          </Field>
        </div>
        <label className="col-span-2 text-sm text-fg-dim max-[900px]:col-span-1">Capacity / validation evidence
          <textarea className={`${inputClass} mt-1 min-h-20 w-full`} value={draft.capacity_evidence} onChange={(event) => setDraft({ ...draft, capacity_evidence: event.target.value })} />
        </label>
        <label className="col-span-2 text-sm text-fg-dim max-[900px]:col-span-1">Behavior-model evidence recipes (comma separated)
          <input
            className={`${inputClass} mt-1 w-full`}
            value={draft.behavior_model_recipes.join(", ")}
            onChange={(event) => setDraft({
              ...draft,
              behavior_model_recipes: event.target.value
                .split(",")
                .map((item) => item.trim())
                .filter(Boolean),
            })}
          />
        </label>
        <label className="col-span-2 text-sm text-fg-dim max-[900px]:col-span-1">Priority channels (comma separated; blank inherits collection default)
          <input
            aria-label="Priority channels"
            className={`${inputClass} mt-1 w-full`}
            value={(draft.priority_channels ?? []).join(", ")}
            onChange={(event) => setDraft({
              ...draft,
              priority_channels: event.target.value.trim() === ""
                ? null
                : event.target.value.split(",").map((item) => item.trim()).filter(Boolean),
            })}
          />
        </label>
      </div>

      <div className="flex flex-wrap gap-5 text-sm">
        <label><input type="checkbox" checked={draft.broadcast_decode} onChange={(event) => setDraft({ ...draft, broadcast_decode: event.target.checked })} /> decode broadcast CAN</label>
        <label><input type="checkbox" checked={draft.experimental} onChange={(event) => setDraft({ ...draft, experimental: event.target.checked })} /> experimental / needs validation</label>
        <label title="Keep the manufacturer diagnostic session open for the whole capture. This requires Mode 01 to be disabled."><input type="checkbox" checked={draft.did_continuous} disabled={!draft.mode22_enabled || draft.mode22.length + (draft.memory?.length ?? 0) === 0} onChange={(event) => setDraft({ ...draft, did_continuous: event.target.checked })} /> hold diagnostic session continuously</label>
        <label title="Every channel selected here is sampled together and shares the round's timestamp, so a capture reads as one row per sample. The true arrival instant of each value is kept alongside."><input type="checkbox" checked={draft.round_timestamps} onChange={(event) => setDraft({ ...draft, round_timestamps: event.target.checked })} /> one timestamp per round (synchronous rows)</label>
        <label title="Off by default: every selected channel is sampled each round, so a capture reads as synchronous rows. Uneven rates suit background monitoring, not tuning."><input type="checkbox" checked={showWeights} onChange={(event) => {
          setShowWeights(event.target.checked);
          // Leaving the picker resets weights to 1 so a half-finished
          // experiment can't silently skew a later capture.
          if (!event.target.checked) {
            setDraft({
              ...draft,
              mode01: Object.fromEntries(Object.keys(draft.mode01).map((key) => [key, 1])),
              mode22_rate_weights: {},
            });
          }
        }} /> per-channel rate weights (advanced)</label>
        <label><input type="checkbox" checked={draft.corpus_eligible} disabled={draft.name === "default"} onChange={(event) => setDraft({ ...draft, corpus_eligible: event.target.checked, storage_prefix: event.target.checked ? "" : `_capture-${draft.name}` })} /> eligible for training corpus</label>
      </div>

      <div className="grid grid-cols-2 gap-4 max-[900px]:grid-cols-1">
        <ChannelPicker title="OBD PIDs (Mode 01)" hint="Standard SAE J1979 parameters. Polled 6 per request; a selection larger than that spans more than one request." showWeights={showWeights} items={pids} selected={mode01Set} onToggle={togglePid} weights={draft.mode01} onWeight={(channel, weight) => setDraft({ ...draft, mode01: { ...draft.mode01, [channel]: weight } })} enabled={draft.mode01_enabled} onEnabled={(value) => setDraft({ ...draft, mode01_enabled: value })} />
        <ChannelPicker title="Manufacturer DIDs (Mode 22)" hint="UDS ReadDataByIdentifier, from this vehicle's DID map." showWeights={showWeights} items={dids} selected={mode22Set} onToggle={toggleDid} weights={draft.mode22_rate_weights} onWeight={(channel, weight) => setDraft({ ...draft, mode22_rate_weights: { ...draft.mode22_rate_weights, [channel]: weight } })} enabled={draft.mode22_enabled} onEnabled={(value) => setDraft({ ...draft, mode22_enabled: value })} />
        <ChannelPicker title="ECU RAM (service 0x23)" hint="ReadMemoryByAddress channels from this vehicle's memory map; uses the Mode 22 diagnostic session." items={memoryChannels} selected={memorySet} onToggle={toggleMemory} enabled={draft.mode22_enabled} />
      </div>

      {draft.mode22_enabled && draft.mode22.length + (draft.memory?.length ?? 0) > 0 && (
        <p className="text-sm text-warn">
          {draft.did_continuous
            ? "This profile holds the manufacturer diagnostic session open. Mode 01 must be disabled because it cannot answer until the session exits."
            : draft.mode01_enabled && Object.keys(draft.mode01).length > 0
              ? "This profile alternates Mode 01 and Mode 22 windows. The active capture status below reports fast-exit successes/failures and achieved channel rates."
              : "This profile enters the vehicle's configured diagnostic session for each Mode 22 window."}
        </p>
      )}
      {warnings.length > 0 && <div className="rounded border border-warn/50 p-2 text-sm text-warn">{warnings.map((warning) => <div key={warning.model}>{warning.model}: missing {warning.missing_channels.join(", ")}</div>)}</div>}
      <div className="flex flex-wrap gap-2">
        <button className={buttonClass} type="button" disabled={!token || !!busy} onClick={() => save(false)}>Save</button>
        <button className={buttonClass} type="button" disabled={!token || !!busy || draft.name === originalName} onClick={() => save(true)}>Save as</button>
        <button className={dangerButtonClass} type="button" disabled={!token || draft.name === "default" || active?.name === draft.name || !!busy} onClick={() => run("capture profile delete", async () => {
          await deleteCaptureProfile(token, draft.name);
          setSelectedName("default");
        }, { then: refresh })}>Delete</button>
      </div>
      </>}
    </section>
  );
}

function ChannelPicker({
  title, items, selected, onToggle, weights, onWeight, enabled, onEnabled, hint, showWeights,
}: {
  title: string;
  items: string[];
  selected: Set<string>;
  onToggle: (item: string) => void;
  weights?: Record<string, number>;
  onWeight?: (item: string, weight: number) => void;
  /** Whole-service switch. Disabling greys the list but keeps the ticks, so
   *  re-enabling doesn't mean rebuilding the selection from scratch. */
  enabled: boolean;
  onEnabled?: (value: boolean) => void;
  hint?: string;
  /** Per-channel rate weights. Off by default: a capture normally wants every
   *  selected channel sampled together, and uneven rates only make sense for
   *  background monitoring, where slow signals shouldn't crowd out fast ones. */
  showWeights?: boolean;
}) {
  return <fieldset className={`max-h-80 overflow-y-auto rounded border border-border p-2 ${enabled ? "" : "opacity-50"}`}>
    <legend className="px-1 text-sm font-semibold">
      <label className="flex items-center gap-2">
        {onEnabled && <input aria-label={`enable ${title}`} type="checkbox" checked={enabled} onChange={(event) => onEnabled(event.target.checked)} />}
        {title}
      </label>
    </legend>
    {hint && <p className="mt-0 mb-1 px-1 text-[0.72rem] text-fg-dim">{hint}</p>}
    {items.map((item) => <label key={item} className="flex min-h-8 items-center gap-2 border-b border-border/40 text-sm">
      <input type="checkbox" checked={selected.has(item)} disabled={!enabled} onChange={() => onToggle(item)} />
      <span className="flex-1">{item}</span>
      {showWeights && selected.has(item) && weights && onWeight && <input aria-label={`${item} rate weight`} className={`${inputClass} w-20`} type="number" min="0.1" step="0.1" disabled={!enabled} value={weights[item] ?? 1} onChange={(event) => onWeight(item, Number(event.target.value))} />}
    </label>)}
  </fieldset>;
}
