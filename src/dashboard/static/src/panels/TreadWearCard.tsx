import { useCallback, useState } from "react";

import { getTires, recordTireEvent } from "../api/http";
import { Panel } from "../components/Panel";
import { Sparkline } from "../components/Sparkline";
import { useControlResource } from "../hooks/useControlResource";
import {
  cn,
  definitionListClass,
  definitionRowClass,
  definitionTermClass,
  definitionValueClass,
  placeholderClass,
} from "../styles";
import type { TireAction, TireStatus } from "../types";
import { TokenRequired } from "../components/TokenRequired";

interface TreadWearCardProps {
  token: string;
}

/** The headline: estimated tread, else why there is none yet. */
export function treadHeadline(status: TireStatus): string {
  const estimate = status.estimate;
  if (estimate) {
    if (estimate.drives_since_reading === 0) {
      return `${estimate.tread_depth_mm.toFixed(1)} mm (the gauge reading; no drive measured since)`;
    }
    return `${estimate.tread_depth_mm.toFixed(1)} mm ± ${estimate.uncertainty_mm.toFixed(1)}`;
  }
  if (status.baseline?.status === "pending") {
    return `gauge reading recorded; ${status.baseline.drives} of 3 drives near it measured`;
  }
  if (!status.current) return "no measured drive yet";
  return "record a gauge reading to estimate tread depth";
}

/** Tire wear from GPS (`/control/tires`): each trip's rolling circumference
 *  on steady cruise, trended. One mm of tread is 2π mm of circumference, so a
 *  gauge reading on the ledger turns the trend into an estimated depth; a
 *  rotation or new tires restarts it. */
export function TreadWearCard({ token }: TreadWearCardProps) {
  const fetcher = useCallback((signal: AbortSignal) => getTires(token, signal), [token]);
  const { data, error, loading, refresh } = useControlResource(fetcher, {
    enabled: Boolean(token),
    intervalMs: 60000,
  });
  const [action, setAction] = useState<TireAction>("tread_measured");
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  if (!token) {
    return (
      <Panel title="Tread wear">
        <TokenRequired purpose="view tire wear" />
      </Panel>
    );
  }
  if (loading && !data) {
    return (
      <Panel title="Tread wear">
        <p className={placeholderClass}>loading…</p>
      </Panel>
    );
  }
  if (error || !data) {
    return (
      <Panel title="Tread wear">
        <p className={cn(placeholderClass, "text-bad")}>{error ?? "no data"}</p>
      </Panel>
    );
  }

  const needsValue = action === "tread_measured" || action === "pressure_changed";
  const record = async () => {
    const number = Number(value);
    if (needsValue && !(number > 0)) {
      setSaveError(
        action === "tread_measured" ? "enter the depth in mm" : "enter the pressure in psi",
      );
      return;
    }
    setSaving(true);
    setSaveError(null);
    try {
      await recordTireEvent(token, {
        occurredAt: new Date().toISOString().slice(0, 10),
        action,
        treadDepthMm: action === "tread_measured" ? number : undefined,
        pressurePsi: action === "pressure_changed" ? number : undefined,
      });
      setValue("");
      refresh();
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const series = data.drives.map((d) => d.circumference_m);
  return (
    <Panel title="Tread wear">
      <dl className={definitionListClass}>
        <div className={definitionRowClass}>
          <dt className={definitionTermClass}>est. tread</dt>
          <dd className={definitionValueClass} data-testid="tread-headline">
            {treadHeadline(data)}
            {data.estimate && data.baseline && (
              <span className="text-fg-dim">
                {" "}
                · worn {data.estimate.worn_since_baseline_mm.toFixed(1)} mm since the{" "}
                {data.baseline.measured_on} gauge reading ({data.baseline.tread_depth_mm} mm)
              </span>
            )}
          </dd>
        </div>
        {data.current && (
          <div className={definitionRowClass}>
            <dt className={definitionTermClass}>rolling circ.</dt>
            <dd className={definitionValueClass}>
              {data.current.circumference_m.toFixed(4)} m ± {data.current.uncertainty_m.toFixed(4)}
              <span className="text-fg-dim">
                {" "}
                · last {data.current.drives} drive(s), scatter {data.current.drive_scatter_pct}% per
                drive
              </span>
            </dd>
          </div>
        )}
        {series.length > 1 && (
          <div className={definitionRowClass}>
            <dt className={definitionTermClass}>drives</dt>
            <dd className={definitionValueClass}>
              <Sparkline values={series} width={160} height={28} />
              <span className="text-fg-dim"> {series.length} measured</span>
            </dd>
          </div>
        )}
        {data.series_start && (
          <div className={definitionRowClass}>
            <dt className={definitionTermClass}>since</dt>
            <dd className={definitionValueClass}>
              {data.series_start}
              {data.series_reset_by && (
                <span className="text-fg-dim"> ({data.series_reset_by.title})</span>
              )}
            </dd>
          </div>
        )}
      </dl>
      {data.caveats.length > 0 && (
        <ul className="mt-2 list-disc pl-5 text-[0.8rem] text-fg-dim">
          {data.caveats.map((caveat) => (
            <li key={caveat}>{caveat}</li>
          ))}
        </ul>
      )}
      <div className="mt-3 flex flex-wrap items-center gap-2 text-[0.82rem]">
        <select
          aria-label="tire event"
          value={action}
          onChange={(e) => setAction(e.target.value as TireAction)}
          className="rounded border border-border bg-bg px-1 py-0.5"
        >
          <option value="tread_measured">gauge reading (mm)</option>
          <option value="pressure_changed">pressure set (psi)</option>
          <option value="rotated">tires rotated</option>
          <option value="replaced">tires replaced</option>
        </select>
        {needsValue && (
          <input
            aria-label="value"
            inputMode="decimal"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            className="w-20 rounded border border-border bg-bg px-1 py-0.5"
          />
        )}
        <button
          type="button"
          disabled={saving}
          onClick={() => void record()}
          className="rounded border border-border px-2 py-0.5 hover:bg-bg-hover disabled:opacity-50"
        >
          {saving ? "saving…" : "record"}
        </button>
        {saveError && <span className="text-bad">{saveError}</span>}
      </div>
    </Panel>
  );
}
