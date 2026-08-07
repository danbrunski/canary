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
  CaptureProfileWarning,
  StatusResponse,
} from "../types";

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
    mode01_enabled: true,
    mode22_enabled: true,
    did_interval_seconds: null,
    round_timestamps: true,
    target_rate_hz: null,
    did_continuous: false,
    bus_budget_override: null,
    expected_total_demand_hz: null,
    capacity_evidence: "",
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
}: {
  token: string;
  status: StatusResponse | null;
  /** Live values, so a running capture shows what it is recording. */
  channels?: Map<string, ChannelSnapshot>;
}) {
  const [profiles, setProfiles] = useState<CaptureProfile[]>([]);
  const [selectedName, setSelectedName] = useState("default");
  const [draft, setDraft] = useState<CaptureProfile>(blankProfile());
  const [originalName, setOriginalName] = useState<string | undefined>();
  const [pids, setPids] = useState<string[]>([]);
  const [dids, setDids] = useState<string[]>([]);
  const [warnings, setWarnings] = useState<CaptureProfileWarning[]>([]);
  const [tuneRevision, setTuneRevision] = useState("");
  const [showWeights, setShowWeights] = useState(false);
  const { notice, setNotice, busy, run } = useAsyncAction();

  const refresh = useCallback(async () => {
    if (!token) return;
    const [catalog, obd, didMap] = await Promise.all([
      getCaptureProfiles(token),
      getObdCoverage(),
      getDids(),
    ]);
    setProfiles(catalog.profiles);
    setPids(
      obd.rows
        .filter((row) => row.channel && row.status === "decoded")
        .map((row) => row.channel as string)
        .sort(),
    );
    setDids(didMap.dids.map((entry) => entry.name).sort());
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

  const active = status?.capture_profile;
  const paused = active?.paused ?? false;
  const selectedProfile = profiles.find((profile) => profile.name === selectedName);
  // Starting is intentionally name-only: the daemon reloads the authoritative
  // profile from disk.  Never present an edited draft as the plan that request
  // will launch, and require a save before the button becomes actionable.
  const plan = active?.active ? active : (selectedProfile ?? draft);
  const hasUnsavedChanges = selectedProfile == null
    || JSON.stringify(draft) !== JSON.stringify(selectedProfile);
  const scheduler = status?.health?.obd_scheduler;
  const achievedRates = Object.entries(scheduler?.achieved_hz_capture ?? {}).sort(
    ([a], [b]) => a.localeCompare(b),
  );
  const maxGaps = scheduler?.capture_max_gap_seconds ?? {};
  const didWindow = scheduler?.did_profile;
  const mode01Count = plan.mode01_enabled ? Object.keys(plan.mode01).length : 0;
  const mode22Count = plan.mode22_enabled ? plan.mode22.length : 0;
  const sessionPolicy = mode22Count === 0
    ? "Mode 01 only"
    : plan.did_continuous
      ? "Mode 22 session held open"
      : mode01Count > 0
        ? "Mode 01 / Mode 22 alternating"
        : "Mode 22 windowed";

  const togglePause = useCallback(() => {
    if (!token || !active?.active) return;
    void run(paused ? "capture resume" : "capture pause", async () => {
      await pauseProfileCapture(token, !paused);
    });
  }, [token, active?.active, paused, run]);

  const togglePauseRef = useRef(togglePause);
  togglePauseRef.current = togglePause;

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
  const mode01Set = useMemo(
    () => new Set("AUTO" in draft.mode01 ? pids : Object.keys(draft.mode01)),
    [draft.mode01, pids],
  );
  const mode22Set = useMemo(() => new Set(draft.mode22), [draft.mode22]);

  /** Latest value of each channel the *running* capture selected. */
  const liveValues = useMemo(() => {
    if (!active?.active || !channels) return [];
    const wanted = [...Object.keys(active.mode01), ...active.mode22];
    return wanted.flatMap((channel) => {
      const snapshot =
        channels.get(channel) ?? channels.get(`obd:${channel}`) ?? channels.get(`did:${channel}`);
      if (!snapshot) return [];
      const value = snapshot.latest.value;
      return [{
        channel,
        value: typeof value === "number" ? value.toFixed(2) : String(value ?? "—"),
        unit: snapshot.latest.unit ?? "",
      }];
    });
  }, [active?.active, active?.mode01, active?.mode22, channels]);

  const togglePid = (channel: string) => {
    const next = "AUTO" in draft.mode01
      ? Object.fromEntries(pids.map((name) => [name, 1]))
      : { ...draft.mode01 };
    if (channel in next) delete next[channel];
    else next[channel] = 1;
    setDraft({ ...draft, mode01: next });
  };
  const toggleDid = (name: string) => {
    setDraft({
      ...draft,
      mode22: mode22Set.has(name)
        ? draft.mode22.filter((item) => item !== name)
        : [...draft.mode22, name],
    });
  };

  const save = (saveAs: boolean) =>
    run(saveAs ? "capture profile save-as" : "capture profile save", async () => {
      const result = await saveCaptureProfile(token, draft, saveAs ? undefined : originalName);
      setWarnings(result.warnings);
      setSelectedName(result.profile.name);
    }, { then: refresh });

  return (
    <section className={`${panelClass} flex flex-col gap-4`}>
      <header className={panelHeaderClass}>
        <h2 className={panelTitleClass}>Capture profiles</h2>
        <span className={active?.active ? "text-warn" : "text-fg-dim"}>
          {active?.active ? `active: ${active.name}` : "default collection"}
        </span>
      </header>
      <Notice notice={notice} />
      <Notice notice={busy ? { kind: "busy", text: `Working: ${busy}` } : null} />

      <div className="flex flex-wrap gap-2">
        <select className={inputClass} value={selectedName} onChange={(event) => setSelectedName(event.target.value)}>
          {profiles.map((profile) => <option key={profile.name}>{profile.name}</option>)}
        </select>
        <button className={buttonClass} type="button" onClick={() => {
          const next = blankProfile();
          setDraft(next);
          setOriginalName(undefined);
          setSelectedName("");
        }}>New</button>
        <input className={inputClass} value={tuneRevision} onChange={(event) => setTuneRevision(event.target.value)} placeholder="ROM / tune revision note" />
        {!active?.active ? (
          <button
            className={buttonClass}
            type="button"
            title={hasUnsavedChanges ? "Save profile changes before starting" : undefined}
            disabled={!token || selectedProfile?.name === "default" || hasUnsavedChanges || !!busy}
            onClick={() => run("capture start", async () => {
              await startProfileCapture(token, selectedProfile!.name, tuneRevision || undefined);
            })}
          >Start</button>
        ) : (
          <>
            <button className={buttonClass} type="button" disabled={!token || !!busy} onClick={togglePause}>
              {paused ? "Resume" : "Pause"} <span className="text-fg-dim">(space)</span>
            </button>
            <button className={buttonClass} type="button" disabled={!token || !!busy} onClick={() => run("capture stop", async () => {
              await stopProfileCapture(token);
            })}>End capture</button>
          </>
        )}
      </div>

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
          <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-fg-dim">
            <span>{mode01Count} Mode 01 channels</span>
            <span>{mode22Count} Mode 22 DIDs</span>
            <span>{plan.target_rate_hz == null ? "relative cadence" : `${plan.target_rate_hz} Hz/channel`}</span>
            <span>{sessionPolicy}</span>
            {plan.did_interval_seconds != null && <span>DID window every {plan.did_interval_seconds}s</span>}
            {plan.bus_budget_override != null && <span>{plan.bus_budget_override} requests/s budget</span>}
            {plan.expected_total_demand_hz != null && <span>{plan.expected_total_demand_hz} expected channel reads/s</span>}
            <span>{plan.corpus_eligible ? "training eligible" : "training corpus excluded"}</span>
            {plan.behavior_model_recipes.length > 0 && (
              <span>{plan.behavior_model_recipes.length} behavior-model recipes allowed</span>
            )}
          </div>
          {plan.capacity_evidence && <p className="mt-2 text-xs text-fg-dim">{plan.capacity_evidence}</p>}
        </div>
      )}

      {!active?.active && hasUnsavedChanges && draft.name !== "default" && (
        <p className="text-xs text-warn">Save this profile before starting it.</p>
      )}

      {active?.active && (
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
                  {!didWindow.fast_exit_supported && " · fast exit unavailable"}
                </p>
              )}
              {achievedRates.length > 0 && (
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
        <label className="text-sm text-fg-dim">Bus budget (requests/s; blank = default)
          <input className={`${inputClass} mt-1 w-full`} type="number" min="2" max="200" value={draft.bus_budget_override ?? ""} onChange={(event) => setDraft({ ...draft, bus_budget_override: event.target.value ? Number(event.target.value) : null })} />
        </label>
        <label className="text-sm text-fg-dim">Target rate (Hz/channel; blank = relative cadence)
          <input className={`${inputClass} mt-1 w-full`} type="number" min="0.1" step="0.1" value={draft.target_rate_hz ?? ""} onChange={(event) => setDraft({ ...draft, target_rate_hz: event.target.value ? Number(event.target.value) : null })} />
        </label>
        <label className="text-sm text-fg-dim">Expected total demand (channel reads/s)
          <input className={`${inputClass} mt-1 w-full`} type="number" min="0.1" step="0.1" value={draft.expected_total_demand_hz ?? ""} onChange={(event) => setDraft({ ...draft, expected_total_demand_hz: event.target.value ? Number(event.target.value) : null })} />
        </label>
        <label className="text-sm text-fg-dim">Mode 22 window interval (s; blank = daemon default)
          <input className={`${inputClass} mt-1 w-full`} type="number" min="0.1" step="0.1" disabled={!draft.mode22_enabled} value={draft.did_interval_seconds ?? ""} onChange={(event) => setDraft({ ...draft, did_interval_seconds: event.target.value ? Number(event.target.value) : null })} />
        </label>
        <label className="text-sm text-fg-dim">Storage prefix (must be <code>_capture-&lt;slug&gt;</code>)
          <input className={`${inputClass} mt-1 w-full`} value={draft.storage_prefix} disabled={draft.name === "default" || draft.corpus_eligible} onChange={(event) => setDraft({ ...draft, storage_prefix: event.target.value })} />
        </label>
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
      </div>

      <div className="flex flex-wrap gap-5 text-sm">
        <label><input type="checkbox" checked={draft.broadcast_decode} onChange={(event) => setDraft({ ...draft, broadcast_decode: event.target.checked })} /> decode broadcast CAN</label>
        <label><input type="checkbox" checked={draft.experimental} onChange={(event) => setDraft({ ...draft, experimental: event.target.checked })} /> experimental / needs validation</label>
        <label title="Keep the manufacturer diagnostic session open for the whole capture. This requires Mode 01 to be disabled."><input type="checkbox" checked={draft.did_continuous} disabled={!draft.mode22_enabled || draft.mode22.length === 0} onChange={(event) => setDraft({ ...draft, did_continuous: event.target.checked })} /> hold Mode 22 session continuously</label>
        <label title="Every channel selected here is sampled together and shares the round's timestamp, so a capture reads as one row per sample. The true arrival instant of each value is kept alongside."><input type="checkbox" checked={draft.round_timestamps} onChange={(event) => setDraft({ ...draft, round_timestamps: event.target.checked })} /> one timestamp per round (synchronous rows)</label>
        <label title="Off by default: every selected channel is sampled each round, so a capture reads as synchronous rows. Uneven rates suit background monitoring, not tuning."><input type="checkbox" checked={showWeights} onChange={(event) => {
          setShowWeights(event.target.checked);
          // Leaving the picker resets weights to 1 so a half-finished
          // experiment can't silently skew a later capture.
          if (!event.target.checked) {
            setDraft({ ...draft, mode01: Object.fromEntries(Object.keys(draft.mode01).map((key) => [key, 1])) });
          }
        }} /> per-channel rate weights (advanced)</label>
        <label><input type="checkbox" checked={draft.corpus_eligible} disabled={draft.name === "default"} onChange={(event) => setDraft({ ...draft, corpus_eligible: event.target.checked, storage_prefix: event.target.checked ? "" : `_capture-${draft.name}` })} /> eligible for training corpus</label>
      </div>

      <div className="grid grid-cols-2 gap-4 max-[900px]:grid-cols-1">
        <ChannelPicker title="OBD PIDs (Mode 01)" hint="Standard SAE J1979 parameters. Polled 6 per request; a selection larger than that spans more than one request." showWeights={showWeights} items={pids} selected={mode01Set} onToggle={togglePid} weights={draft.mode01} onWeight={(channel, weight) => setDraft({ ...draft, mode01: { ...draft.mode01, [channel]: weight } })} enabled={draft.mode01_enabled} onEnabled={(value) => setDraft({ ...draft, mode01_enabled: value })} />
        <ChannelPicker title="Manufacturer DIDs (Mode 22)" hint="UDS ReadDataByIdentifier, from this vehicle's DID map." items={dids} selected={mode22Set} onToggle={toggleDid} enabled={draft.mode22_enabled} onEnabled={(value) => setDraft({ ...draft, mode22_enabled: value })} />
      </div>

      {draft.mode22_enabled && draft.mode22.length > 0 && (
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
  onEnabled: (value: boolean) => void;
  hint?: string;
  /** Per-channel rate weights. Off by default: a capture normally wants every
   *  selected channel sampled together, and uneven rates only make sense for
   *  background monitoring, where slow signals shouldn't crowd out fast ones. */
  showWeights?: boolean;
}) {
  return <fieldset className={`max-h-80 overflow-y-auto rounded border border-border p-2 ${enabled ? "" : "opacity-50"}`}>
    <legend className="px-1 text-sm font-semibold">
      <label className="flex items-center gap-2">
        <input aria-label={`enable ${title}`} type="checkbox" checked={enabled} onChange={(event) => onEnabled(event.target.checked)} />
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
