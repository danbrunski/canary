import { useMemo, useRef, useState } from "react";

import { Panel } from "../components/Panel";
import { Sparkline } from "../components/Sparkline";
import { useLocalStorageJson } from "../hooks/useLocalStorage";
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import { buttonClass, cn, inputClass, placeholderClass } from "../styles";
import { downloadText } from "../util/browser";

interface CustomDashboardPanelProps {
  channels: Map<string, ChannelSnapshot>;
}

interface Profile {
  title: string;
  tiles: string[]; // channel keys
}

const STORAGE_KEY = "canary.dashboard";
const DEFAULT_PROFILE: Profile = { title: "My dashboard", tiles: [] };

interface PresetGroup {
  label: string;
  channels: string[];
}

//: One-tap channel bundles for the picker below. AFR / fueling (Task 2.8b)
//: is the first group: the wideband λ PIDs (0x24/0x25/0x34/0x35, Task 2.1),
//: closed- and open-loop fuel trims, and the live fuel-rate/economy
//: `derived:*` channels (Task 1.1) that stream over `/ws/telemetry`. A
//: preset only adds the channels actually present in `numericKeys` below —
//: no placeholder tiles for PIDs this vehicle/ECU doesn't answer.
const PRESET_GROUPS: PresetGroup[] = [
  {
    label: "AFR / fueling",
    channels: [
      "obd:O2_WR_LAMBDA_S1",
      "obd:O2_WR_LAMBDA_S2",
      "obd:O2_WR_CURRENT_S1",
      "obd:O2_WR_CURRENT_S2",
      "obd:COMMANDED_EQUIV_RATIO",
      "obd:SHORT_FUEL_TRIM_1",
      "obd:SHORT_FUEL_TRIM_2",
      "obd:LONG_FUEL_TRIM_1",
      "obd:LONG_FUEL_TRIM_2",
      "obd:ENGINE_FUEL_RATE",
      "derived:FUEL_RATE_LPH",
      "derived:INSTANT_ECONOMY_MPG",
      "derived:INSTANT_ECONOMY_L_PER_100KM",
    ],
  },
];

/** Custom dashboard builder (Task 6.9). A user-configurable gauge page:
 *  pick channels → big digital readouts with a trend sparkline, persisted
 *  per browser and export/importable as a JSON profile. (No drag-resize —
 *  that needs a pointer + visual verification this environment can't give;
 *  the channel set + responsive grid is the durable core a richer builder
 *  layers onto.) */
export function CustomDashboardPanel({ channels }: CustomDashboardPanelProps) {
  const [profile, setProfile] = useLocalStorageJson<Profile>(STORAGE_KEY, DEFAULT_PROFILE);
  const [filter, setFilter] = useState("");
  const [importing, setImporting] = useState(false);
  const [importText, setImportText] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  const numericKeys = useMemo(
    () =>
      [...channels.entries()]
        .filter(([, s]) => typeof s.latest.value === "number")
        .map(([k]) => k)
        .sort(),
    [channels],
  );

  const addTile = (key: string) =>
    setProfile((p) => (p.tiles.includes(key) ? p : { ...p, tiles: [...p.tiles, key] }));
  const removeTile = (key: string) =>
    setProfile((p) => ({ ...p, tiles: p.tiles.filter((k) => k !== key) }));

  // Only add channels the vehicle is actually reporting — a preset never
  // seeds a dead tile for a PID this ECU doesn't answer.
  const addPreset = (group: PresetGroup) => {
    const live = group.channels.filter((k) => numericKeys.includes(k));
    setProfile((p) => ({
      ...p,
      tiles: [...p.tiles, ...live.filter((k) => !p.tiles.includes(k))],
    }));
  };

  const exportProfile = () => {
    downloadText(JSON.stringify(profile, null, 2), "canary-dashboard.json", "application/json");
  };

  const applyImport = (text: string) => {
    try {
      const parsed = JSON.parse(text) as Profile;
      if (parsed && Array.isArray(parsed.tiles)) {
        setProfile({
          title: typeof parsed.title === "string" ? parsed.title : "Imported",
          tiles: parsed.tiles.filter((t) => typeof t === "string"),
        });
        setImporting(false);
        setImportText("");
      }
    } catch {
      /* leave the editor open so the operator can fix the JSON */
    }
  };

  const shown = filter
    ? numericKeys.filter((k) => k.toLowerCase().includes(filter.toLowerCase()))
    : numericKeys;

  return (
    <Panel
      title="Builder"
      count={`${profile.tiles.length} tiles`}
      actions={
        <div className="flex items-center gap-2">
          <button
            type="button"
            className={cn(buttonClass, "px-2.5 py-1 text-[0.74rem]")}
            onClick={exportProfile}
          >
            Export
          </button>
          <button
            type="button"
            className={cn(buttonClass, "px-2.5 py-1 text-[0.74rem]")}
            onClick={() => setImporting((v) => !v)}
          >
            Import
          </button>
        </div>
      }
    >
      <input
        className={cn(inputClass, "mb-3 w-full max-w-sm")}
        value={profile.title}
        onChange={(e) => setProfile((p) => ({ ...p, title: e.target.value }))}
        aria-label="Dashboard title"
      />

      {importing && (
        <div className="mb-3 flex flex-col gap-2">
          <textarea
            className={cn(inputClass, "h-28 w-full resize-y font-num text-[0.78rem]")}
            value={importText}
            placeholder="paste a dashboard profile JSON…"
            onChange={(e) => setImportText(e.target.value)}
          />
          <div className="flex gap-2">
            <button type="button" className={buttonClass} onClick={() => applyImport(importText)}>
              Apply JSON
            </button>
            <input
              ref={fileRef}
              type="file"
              accept="application/json,.json"
              className="hidden"
              onChange={async (e) => {
                const f = e.target.files?.[0];
                if (f) applyImport(await f.text());
              }}
            />
            <button type="button" className={buttonClass} onClick={() => fileRef.current?.click()}>
              From file…
            </button>
          </div>
        </div>
      )}

      {profile.tiles.length === 0 ? (
        <p className={placeholderClass}>
          empty dashboard — pick channels below to build your gauge page.
        </p>
      ) : (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(11rem,1fr))] gap-2">
          {profile.tiles.map((key) => {
            const snap = channels.get(key);
            const v = snap?.latest.value;
            return (
              <div
                key={key}
                className="relative rounded-[8px] border border-border-soft bg-[linear-gradient(180deg,var(--color-elev-2),var(--color-elev))] p-3"
              >
                <button
                  type="button"
                  className="absolute right-1.5 top-1.5 text-[0.7rem] text-fg-faint hover:text-bad"
                  onClick={() => removeTile(key)}
                  aria-label={`remove ${key}`}
                >
                  ✕
                </button>
                <div className="truncate text-[0.62rem] uppercase tracking-[0.12em] text-fg-faint">
                  {key}
                </div>
                <div className="font-num text-2xl tabular-nums text-accent">
                  {typeof v === "number" ? v.toFixed(2) : (v ?? "—")}
                  <span className="ml-1 text-[0.7rem] text-fg-faint">
                    {snap?.latest.unit ?? ""}
                  </span>
                </div>
                {snap && snap.history.length > 1 && (
                  <Sparkline values={snap.history.slice(-60)} width={150} height={28} />
                )}
              </div>
            );
          })}
        </div>
      )}

      <div className="mt-4">
        <div className="mb-2 flex flex-wrap gap-1.5">
          {PRESET_GROUPS.map((group) => (
            <button
              key={group.label}
              type="button"
              className={cn(buttonClass, "px-2.5 py-1 text-[0.74rem]")}
              onClick={() => addPreset(group)}
            >
              + {group.label}
            </button>
          ))}
        </div>
        <input
          className={cn(inputClass, "mb-2 w-full")}
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="add channels — filter…"
          aria-label="Filter channels"
        />
        <div className="grid max-h-40 grid-cols-[repeat(auto-fill,minmax(13rem,1fr))] gap-1 overflow-y-auto">
          {shown.map((key) => {
            const on = profile.tiles.includes(key);
            return (
              <button
                key={key}
                type="button"
                className={cn(
                  "truncate rounded border px-2 py-1 text-left text-[0.78rem]",
                  on
                    ? "border-accent bg-accent/10 text-fg"
                    : "border-border-soft text-fg-dim hover:border-border",
                )}
                onClick={() => (on ? removeTile(key) : addTile(key))}
              >
                {on ? "● " : "○ "}
                {key}
              </button>
            );
          })}
        </div>
      </div>
    </Panel>
  );
}
