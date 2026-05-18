import { useMemo, useRef, useState } from "react";

import { Panel } from "../components/Panel";
import { Sparkline } from "../components/Sparkline";
import { useLocalStorageJson } from "../hooks/useLocalStorage";
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import { buttonClass, cn, inputClass, placeholderClass } from "../styles";

interface CustomDashboardPanelProps {
  channels: Map<string, ChannelSnapshot>;
}

interface Profile {
  title: string;
  tiles: string[]; // channel keys
}

const STORAGE_KEY = "canary.dashboard";
const DEFAULT_PROFILE: Profile = { title: "My dashboard", tiles: [] };

/** Custom dashboard builder (Task 6.9). A user-configurable gauge page:
 *  pick channels → big digital readouts with a trend sparkline, persisted
 *  per browser and export/importable as a JSON profile. (No drag-resize —
 *  that needs a pointer + visual verification this environment can't give;
 *  the channel set + responsive grid is the durable core a richer builder
 *  layers onto.) */
export function CustomDashboardPanel({ channels }: CustomDashboardPanelProps) {
  const [profile, setProfile] = useLocalStorageJson<Profile>(
    STORAGE_KEY,
    DEFAULT_PROFILE,
  );
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
    setProfile((p) =>
      p.tiles.includes(key) ? p : { ...p, tiles: [...p.tiles, key] },
    );
  const removeTile = (key: string) =>
    setProfile((p) => ({ ...p, tiles: p.tiles.filter((k) => k !== key) }));

  const exportProfile = () => {
    const blob = new Blob([JSON.stringify(profile, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "canary-dashboard.json";
    a.click();
    URL.revokeObjectURL(url);
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
            <button
              type="button"
              className={buttonClass}
              onClick={() => applyImport(importText)}
            >
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
            <button
              type="button"
              className={buttonClass}
              onClick={() => fileRef.current?.click()}
            >
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
