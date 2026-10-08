// Authoring capture profiles (R08.6, the editor half of CaptureProfilesPanel):
// System → Capture profiles. Draft, service toggles, channel pickers, save and
// delete. Starting a log is CaptureRunner's job (Engine Tuning → Logging); the
// editor has no Start, so an unsaved draft can never be launched.

import { useMemo, useState } from "react";

import { deleteCaptureProfile, saveCaptureProfile } from "../api/http";
import { Notice } from "../components/Notice";
import { Panel } from "../components/Panel";
import { useAsyncAction } from "../hooks/useAsyncAction";
import { useCaptureCatalog } from "../hooks/useCaptureCatalog";
import { buttonClass, dangerButtonClass, inputClass } from "../styles";
import type { CaptureProfile, CaptureProfileWarning, StatusResponse } from "../types";
import { CapturePlanSummary, CollectionStatus, Field, ProfilePicker } from "./captureProfileParts";

/** Numeric settings are 2–4 digits. Sizing them to the grid column made a
 *  bus-budget box span half the page and read as a free-text field. */
const numFieldClass = `${inputClass} mt-1 w-24 text-right`;

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

export const blankProfile = (): CaptureProfile => {
  const name = timestampSlug();
  return {
    name,
    description: "",
    experimental: false,
    mode01: {},
    mode22: [],
    memory: [],
    mode22_rate_weights: {},
    memory_rate_weights: {},
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
    corpus_eligible: false,
    behavior_model_recipes: [],
    storage_prefix: `_capture-${name}`,
  };
};

/** `event.target.value` of a number field as a number, blank as null. */
const numberOrBlank = (value: string): number | null => (value ? Number(value) : null);

export function CaptureProfileEditor({
  token,
  status,
}: {
  token: string;
  status: StatusResponse | null;
}) {
  const { catalog, error: catalogError, refresh } = useCaptureCatalog(token);
  const profiles = useMemo(() => catalog?.profiles ?? [], [catalog]);
  const [selectedName, setSelectedName] = useState("default");
  const [draft, setDraft] = useState<CaptureProfile>(blankProfile);
  const [originalName, setOriginalName] = useState<string | undefined>();
  // The saved profile the draft was last loaded from; a new catalog (after a
  // save) or another pick reloads it. `null` after "New profile".
  const [loadedFrom, setLoadedFrom] = useState<CaptureProfile | null>(null);
  const [warnings, setWarnings] = useState<CaptureProfileWarning[]>([]);
  const [showWeights, setShowWeights] = useState(false);
  const { notice, busy, run } = useAsyncAction();

  const selectedProfile = profiles.find((profile) => profile.name === selectedName);
  if (selectedProfile && selectedProfile !== loadedFrom) {
    setLoadedFrom(selectedProfile);
    setDraft(structuredClone(selectedProfile));
    setOriginalName(selectedProfile.name);
    setWarnings([]);
  }

  const active = status?.capture_profile ?? null;
  const plan = selectedProfile ?? draft;
  const pids = useMemo(() => catalog?.pids ?? [], [catalog]);
  const mode01Set = useMemo(
    () => new Set("AUTO" in draft.mode01 ? pids : Object.keys(draft.mode01)),
    [draft.mode01, pids],
  );
  const mode22Set = useMemo(() => new Set(draft.mode22), [draft.mode22]);
  const memorySet = useMemo(() => new Set(draft.memory ?? []), [draft.memory]);

  const togglePid = (channel: string) => {
    const next =
      "AUTO" in draft.mode01
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
      mode22: selected ? draft.mode22.filter((item) => item !== name) : [...draft.mode22, name],
      mode22_rate_weights: weights,
    });
  };
  const toggleMemory = (name: string) => {
    const selected = memorySet.has(name);
    // The server refuses a weight on a channel `memory` does not select.
    const weights = { ...(draft.memory_rate_weights ?? {}) };
    if (selected) delete weights[name];
    setDraft({
      ...draft,
      memory: selected
        ? (draft.memory ?? []).filter((item) => item !== name)
        : [...(draft.memory ?? []), name],
      memory_rate_weights: weights,
    });
  };

  const save = (saveAs: boolean) =>
    run(
      saveAs ? "capture profile save-as" : "capture profile save",
      async () => {
        const result = await saveCaptureProfile(token, draft, saveAs ? undefined : originalName);
        setWarnings(result.warnings);
        setSelectedName(result.profile.name);
      },
      { then: refresh },
    );

  const remove = () =>
    run(
      "capture profile delete",
      async () => {
        await deleteCaptureProfile(token, draft.name);
        setSelectedName("default");
      },
      { then: refresh },
    );

  const newProfile = () => {
    setDraft(blankProfile());
    setOriginalName(undefined);
    setLoadedFrom(null);
    setSelectedName("");
  };

  return (
    <Panel
      title="Capture profiles"
      actions={<CollectionStatus active={active} />}
      className="flex flex-col gap-4"
    >
      <Notice notice={notice} />
      <Notice notice={busy ? { kind: "busy", text: `Working: ${busy}` } : null} />
      {catalogError && <Notice notice={{ kind: "error", text: catalogError }} />}
      {token && catalog && profiles.length === 0 && (
        <p className="text-sm text-bad">No capture profiles — the catalog failed to load.</p>
      )}

      <p className="text-sm text-fg-dim">
        Define the default collection and temporary logging schemas here. Start a log from Engine
        Tuning → Logging.
      </p>
      <div className="flex flex-wrap gap-2">
        <ProfilePicker
          profiles={profiles}
          value={selectedName}
          onChange={setSelectedName}
          hint="choose a profile to edit"
        />
      </div>

      {plan.name !== "default" && (
        <CapturePlanSummary plan={plan} resolved={catalog?.resolvedPlans[plan.name]} />
      )}

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
          title="Only needed to create an additional profile. To change an existing one, pick it from Profile above."
          onClick={newProfile}
        >
          New profile
        </button>
      </div>

      <div className="grid grid-cols-2 gap-3 max-[900px]:grid-cols-1">
        <label className="text-sm text-fg-dim">
          Name
          <input
            className={`${inputClass} mt-1 w-full`}
            value={draft.name}
            disabled={draft.name === "default"}
            onChange={(event) => {
              const name = event.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "");
              // Carry an auto-derived prefix along with the rename, so captures
              // don't land under the name the profile used to have. A prefix the
              // operator typed themselves is left alone.
              const derived = draft.storage_prefix === `_capture-${draft.name}`;
              setDraft({
                ...draft,
                name,
                storage_prefix:
                  derived && !draft.corpus_eligible ? `_capture-${name}` : draft.storage_prefix,
              });
            }}
          />
        </label>
        <label className="text-sm text-fg-dim">
          Description
          <input
            className={`${inputClass} mt-1 w-full`}
            value={draft.description}
            onChange={(event) => setDraft({ ...draft, description: event.target.value })}
          />
        </label>
        {/* Numeric settings are a handful of digits — sized to fit, not to the
            column, so they stop reading as free-text fields. */}
        <div className="col-span-2 flex flex-wrap gap-x-4 gap-y-2 max-[900px]:col-span-1">
          <Field label="Bus budget" hint="req/s · blank = default">
            <input
              aria-label="Bus budget"
              className={numFieldClass}
              type="number"
              min="2"
              max="200"
              value={draft.bus_budget_override ?? ""}
              onChange={(event) =>
                setDraft({ ...draft, bus_budget_override: numberOrBlank(event.target.value) })
              }
            />
          </Field>
          <Field label="Target rate" hint="Hz/channel · blank = relative">
            <input
              aria-label="Target rate"
              className={numFieldClass}
              type="number"
              min="0.1"
              step="0.1"
              value={draft.target_rate_hz ?? ""}
              onChange={(event) =>
                setDraft({ ...draft, target_rate_hz: numberOrBlank(event.target.value) })
              }
            />
          </Field>
          <Field label="Expected demand" hint="channel reads/s">
            <input
              aria-label="Expected demand"
              className={numFieldClass}
              type="number"
              min="0.1"
              step="0.1"
              value={draft.expected_total_demand_hz ?? ""}
              onChange={(event) =>
                setDraft({ ...draft, expected_total_demand_hz: numberOrBlank(event.target.value) })
              }
            />
          </Field>
          <Field label="Rate floor" hint="Hz · enforced by the contract">
            <input
              aria-label="Rate floor"
              className={numFieldClass}
              type="number"
              min="0.1"
              step="0.1"
              value={draft.min_achieved_rate_hz ?? ""}
              onChange={(event) =>
                setDraft({ ...draft, min_achieved_rate_hz: numberOrBlank(event.target.value) })
              }
            />
          </Field>
          <Field label="Mode 22 window" hint="seconds · blank = daemon default">
            <input
              aria-label="Mode 22 window"
              className={numFieldClass}
              type="number"
              min="0.1"
              step="0.1"
              disabled={!draft.mode22_enabled}
              value={draft.did_interval_seconds ?? ""}
              onChange={(event) =>
                setDraft({ ...draft, did_interval_seconds: numberOrBlank(event.target.value) })
              }
            />
          </Field>
          <Field label="Storage prefix" hint="must be _capture-<slug>" className="grow">
            <input
              aria-label="Storage prefix"
              className={`${inputClass} mt-1 w-64 max-w-full`}
              value={draft.storage_prefix}
              disabled={draft.name === "default" || draft.corpus_eligible}
              onChange={(event) => setDraft({ ...draft, storage_prefix: event.target.value })}
            />
          </Field>
        </div>
        <label className="col-span-2 text-sm text-fg-dim max-[900px]:col-span-1">
          Capacity / validation evidence
          <textarea
            className={`${inputClass} mt-1 min-h-20 w-full`}
            value={draft.capacity_evidence}
            onChange={(event) => setDraft({ ...draft, capacity_evidence: event.target.value })}
          />
        </label>
        <label className="col-span-2 text-sm text-fg-dim max-[900px]:col-span-1">
          Behavior-model evidence recipes (comma separated)
          <input
            className={`${inputClass} mt-1 w-full`}
            value={draft.behavior_model_recipes.join(", ")}
            onChange={(event) =>
              setDraft({
                ...draft,
                behavior_model_recipes: event.target.value
                  .split(",")
                  .map((item) => item.trim())
                  .filter(Boolean),
              })
            }
          />
        </label>
        <label className="col-span-2 text-sm text-fg-dim max-[900px]:col-span-1">
          Priority channels (comma separated; blank inherits collection default)
          <input
            aria-label="Priority channels"
            className={`${inputClass} mt-1 w-full`}
            value={(draft.priority_channels ?? []).join(", ")}
            onChange={(event) =>
              setDraft({
                ...draft,
                priority_channels:
                  event.target.value.trim() === ""
                    ? null
                    : event.target.value
                        .split(",")
                        .map((item) => item.trim())
                        .filter(Boolean),
              })
            }
          />
        </label>
      </div>

      <div className="flex flex-wrap gap-5 text-sm">
        <label>
          <input
            type="checkbox"
            checked={draft.experimental}
            onChange={(event) => setDraft({ ...draft, experimental: event.target.checked })}
          />{" "}
          experimental / needs validation
        </label>
        <label title="Keep the manufacturer diagnostic session open for the whole capture. This requires Mode 01 to be disabled.">
          <input
            type="checkbox"
            checked={draft.did_continuous}
            disabled={
              !draft.mode22_enabled || draft.mode22.length + (draft.memory?.length ?? 0) === 0
            }
            onChange={(event) => setDraft({ ...draft, did_continuous: event.target.checked })}
          />{" "}
          hold diagnostic session continuously
        </label>
        <label title="Every channel selected here is sampled together and shares the round's timestamp, so a capture reads as one row per sample. The true arrival instant of each value is kept alongside.">
          <input
            type="checkbox"
            checked={draft.round_timestamps}
            onChange={(event) => setDraft({ ...draft, round_timestamps: event.target.checked })}
          />{" "}
          one timestamp per round (synchronous rows)
        </label>
        <label title="Normal collection stores a broadcast CAN frame only when its payload changes (P02.10). Store every frame when the capture needs frame counts, frame rates or sub-second frame timing. Not available on the default profile.">
          <input
            type="checkbox"
            checked={draft.store_every_frame ?? false}
            disabled={draft.name === "default"}
            onChange={(event) => setDraft({ ...draft, store_every_frame: event.target.checked })}
          />{" "}
          store every CAN frame
        </label>
        <label title="Normal collection stores each USB ADC input (fuel pressure) as the mean of five samples, about 100 Hz. Store every sample (503.9 Hz with the v2 firmware) to study the rail's injector pulsation. Five times the sensor rows.">
          <input
            type="checkbox"
            checked={draft.analog_full_rate ?? false}
            onChange={(event) => setDraft({ ...draft, analog_full_rate: event.target.checked })}
          />{" "}
          store every analog sample (fuel-pressure pulsation)
        </label>
        <label title="Off by default: every selected channel is sampled each round, so a capture reads as synchronous rows. Uneven rates suit background monitoring, not tuning.">
          <input
            type="checkbox"
            checked={showWeights}
            onChange={(event) => {
              setShowWeights(event.target.checked);
              // Leaving the picker resets weights to 1 so a half-finished
              // experiment can't silently skew a later capture.
              if (!event.target.checked) {
                setDraft({
                  ...draft,
                  mode01: Object.fromEntries(Object.keys(draft.mode01).map((key) => [key, 1])),
                  mode22_rate_weights: {},
                  memory_rate_weights: {},
                });
              }
            }}
          />{" "}
          per-channel rate weights (advanced)
        </label>
        <label>
          <input
            type="checkbox"
            checked={draft.corpus_eligible}
            disabled={draft.name === "default"}
            onChange={(event) =>
              setDraft({
                ...draft,
                corpus_eligible: event.target.checked,
                storage_prefix: event.target.checked ? "" : `_capture-${draft.name}`,
              })
            }
          />{" "}
          eligible for training corpus
        </label>
      </div>

      <div className="grid grid-cols-2 gap-4 max-[900px]:grid-cols-1">
        <ChannelPicker
          title="OBD PIDs (Mode 01)"
          hint="Standard SAE J1979 parameters. Polled 6 per request; a selection larger than that spans more than one request."
          showWeights={showWeights}
          items={pids}
          selected={mode01Set}
          onToggle={togglePid}
          weights={draft.mode01}
          onWeight={(channel, weight) =>
            setDraft({ ...draft, mode01: { ...draft.mode01, [channel]: weight } })
          }
          enabled={draft.mode01_enabled}
          onEnabled={(value) => setDraft({ ...draft, mode01_enabled: value })}
        />
        <ChannelPicker
          title="Manufacturer DIDs (Mode 22)"
          hint="UDS ReadDataByIdentifier, from this vehicle's DID map."
          showWeights={showWeights}
          items={catalog?.dids ?? []}
          selected={mode22Set}
          onToggle={toggleDid}
          weights={draft.mode22_rate_weights}
          onWeight={(channel, weight) =>
            setDraft({
              ...draft,
              mode22_rate_weights: { ...draft.mode22_rate_weights, [channel]: weight },
            })
          }
          enabled={draft.mode22_enabled}
          onEnabled={(value) => setDraft({ ...draft, mode22_enabled: value })}
        />
        <ChannelPicker
          title="ECU RAM (service 0x23)"
          hint="ReadMemoryByAddress channels from this vehicle's memory map; uses the Mode 22 diagnostic session. Channels sharing a window are one read, which runs at the largest weight among them."
          showWeights={showWeights}
          items={catalog?.memory ?? []}
          selected={memorySet}
          onToggle={toggleMemory}
          weights={draft.memory_rate_weights ?? {}}
          onWeight={(channel, weight) =>
            setDraft({
              ...draft,
              memory_rate_weights: { ...(draft.memory_rate_weights ?? {}), [channel]: weight },
            })
          }
          enabled={draft.mode22_enabled}
        />
      </div>

      {draft.mode22_enabled && draft.mode22.length + (draft.memory?.length ?? 0) > 0 && (
        <p className="text-sm text-warn">
          {draft.did_continuous
            ? "This profile holds the manufacturer diagnostic session open. Mode 01 must be disabled because it cannot answer until the session exits."
            : draft.mode01_enabled && Object.keys(draft.mode01).length > 0
              ? "This profile alternates Mode 01 and Mode 22 windows. The active capture status on Logging reports fast-exit successes/failures and achieved channel rates."
              : "This profile enters the vehicle's configured diagnostic session for each Mode 22 window."}
        </p>
      )}
      {warnings.length > 0 && (
        <div className="rounded border border-warn/50 p-2 text-sm text-warn">
          {warnings.map((warning) => (
            <div key={warning.model}>
              {warning.model}: missing {warning.missing_channels.join(", ")}
            </div>
          ))}
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        <button
          className={buttonClass}
          type="button"
          disabled={!token || !!busy}
          onClick={() => void save(false)}
        >
          Save
        </button>
        <button
          className={buttonClass}
          type="button"
          disabled={!token || !!busy || draft.name === originalName}
          onClick={() => void save(true)}
        >
          Save as
        </button>
        <button
          className={dangerButtonClass}
          type="button"
          disabled={!token || draft.name === "default" || active?.name === draft.name || !!busy}
          onClick={() => void remove()}
        >
          Delete
        </button>
      </div>
    </Panel>
  );
}

function ChannelPicker({
  title,
  items,
  selected,
  onToggle,
  weights,
  onWeight,
  enabled,
  onEnabled,
  hint,
  showWeights,
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
  return (
    <fieldset
      className={`max-h-80 overflow-y-auto rounded border border-border p-2 ${enabled ? "" : "opacity-50"}`}
    >
      <legend className="px-1 text-sm font-semibold">
        <label className="flex items-center gap-2">
          {onEnabled && (
            <input
              aria-label={`enable ${title}`}
              type="checkbox"
              checked={enabled}
              onChange={(event) => onEnabled(event.target.checked)}
            />
          )}
          {title}
        </label>
      </legend>
      {hint && <p className="mt-0 mb-1 px-1 text-[0.72rem] text-fg-dim">{hint}</p>}
      {items.map((item) => (
        <label
          key={item}
          className="flex min-h-8 items-center gap-2 border-b border-border/40 text-sm"
        >
          <input
            type="checkbox"
            checked={selected.has(item)}
            disabled={!enabled}
            onChange={() => onToggle(item)}
          />
          <span className="flex-1">{item}</span>
          {showWeights && selected.has(item) && weights && onWeight && (
            <input
              aria-label={`${item} rate weight`}
              className={`${inputClass} w-20`}
              type="number"
              min="0.1"
              step="0.1"
              disabled={!enabled}
              value={weights[item] ?? 1}
              onChange={(event) => onWeight(item, Number(event.target.value))}
            />
          )}
        </label>
      ))}
    </fieldset>
  );
}
