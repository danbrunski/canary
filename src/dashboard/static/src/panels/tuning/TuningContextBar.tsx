import { useEffect, useState } from "react";

import type { TuningContext } from "../../hooks/useTuningContext";
import { cn } from "../../styles";
import type {
  ReadinessEntry,
  ReadinessKey,
  StatusResponse,
  WorkbenchItem,
} from "../../types";

/** Chips in display order. Every key is always rendered: a chip the daemon
 *  did not report is grey "not reported", never absent and never green. */
export const READINESS_CHIPS: ReadonlyArray<{ key: ReadinessKey; label: string }> = [
  { key: "ups_soc", label: "UPS" },
  { key: "storage_free_mb", label: "Storage" },
  { key: "memory_channels", label: "Memory channels" },
  { key: "calibration", label: "Calibration" },
  { key: "vehicle_state", label: "Vehicle" },
];

export type ChipTone = "ok" | "warn" | "bad" | "unknown";

/** W02.7: unknown is not OK. A null value is grey whatever `ok` says. */
export function chipTone(entry: ReadinessEntry | null | undefined): ChipTone {
  if (!entry || entry.value === null || entry.value === undefined) return "unknown";
  if (entry.ok === true) return "ok";
  if (entry.ok === false) return "bad";
  return "warn";
}

const toneClass: Record<ChipTone, string> = {
  ok: "border-ok/60 text-ok",
  warn: "border-warn/60 text-warn",
  bad: "border-bad/60 text-bad",
  unknown: "border-border text-fg-faint",
};

function chipValue(key: ReadinessKey, value: unknown): string {
  if (value === null || value === undefined) return "not reported";
  if (key === "ups_soc" && typeof value === "number") return `${value.toFixed(0)}%`;
  if (key === "storage_free_mb" && typeof value === "number") {
    return value >= 1024 ? `${(value / 1024).toFixed(1)} GB free` : `${value.toFixed(0)} MB free`;
  }
  if (key === "memory_channels" && typeof value === "object") {
    const counts = value as { passed?: number; failed?: number; total?: number };
    const failed = counts.failed ? `, ${counts.failed} failed` : "";
    return `${counts.passed ?? 0}/${counts.total ?? 0} verified${failed}`;
  }
  if (key === "vehicle_state" && typeof value === "string") return value.replace(/_/g, " ");
  return String(value);
}

/** `HH:MM:SS` since `startedAt`, or null when it cannot be parsed. */
export function elapsedLabel(startedAt: string | null | undefined, now: number): string | null {
  if (!startedAt) return null;
  const start = Date.parse(startedAt);
  if (!Number.isFinite(start)) return null;
  const total = Math.max(0, Math.floor((now - start) / 1000));
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(Math.floor(total / 3600))}:${pad(Math.floor((total % 3600) / 60))}:${pad(total % 60)}`;
}

interface Props {
  status: StatusResponse | null;
  context: TuningContext;
  onNavigate: (item: WorkbenchItem) => void;
}

const segmentClass =
  "inline-flex items-center gap-1 rounded px-1 py-0.5 text-left hover:bg-elev-2 focus-visible:outline focus-visible:outline-accent";

/** The strip across every Engine Tuning page (W02.6): vehicle, running
 *  calibration, capture, selected log, loaded ROM, readiness. Each segment
 *  names its empty state and links to the page that fills it. */
export function TuningContextBar({ status, context, onNavigate }: Props) {
  const [now, setNow] = useState(() => Date.now());
  const [openChip, setOpenChip] = useState<ReadinessKey | null>(null);
  const capture = status?.capture_profile;
  const captureActive = capture?.active === true;
  useEffect(() => {
    if (!captureActive) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [captureActive]);

  const vehicle = status?.profile?.name || status?.vehicle || "vehicle unknown";
  const readiness = status?.tuning_readiness ?? null;
  const openEntry = openChip ? readiness?.[openChip] : undefined;

  return (
    <section
      aria-label="Engine Tuning context"
      className="flex flex-col gap-1.5 rounded-lg border border-border bg-elev px-3 py-2 text-sm"
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="font-semibold">{vehicle}</span>
        <Separator />
        <RunningCalibration status={status} onNavigate={onNavigate} />
        <Separator />
        <button
          type="button"
          className={segmentClass}
          onClick={() => onNavigate("tuning-logging")}
          aria-label="Capture: open Logging"
        >
          {captureActive && capture ? (
            <span>
              capture: {capture.name}{" "}
              <span className="font-num tabular-nums">
                {elapsedLabel(capture.started_at, now) ?? ""}
              </span>
            </span>
          ) : (
            <span className="text-fg-dim">capture: none</span>
          )}
        </button>
        <Separator />
        <button
          type="button"
          className={segmentClass}
          onClick={() => onNavigate("tuning-logs")}
          aria-label="Log: open Logs"
        >
          {context.selectedLogId ? (
            <span>log: {context.selectedLogId}</span>
          ) : (
            <span className="text-fg-dim">log: none selected</span>
          )}
        </button>
        <Separator />
        <button
          type="button"
          className={segmentClass}
          onClick={() => onNavigate("tuning-rom-maps")}
          aria-label="ROM: open ROM Maps"
        >
          {context.loadedRom ? (
            <span>ROM: {context.loadedRom.filename}</span>
          ) : (
            <span className="text-fg-dim">ROM: none loaded</span>
          )}
        </button>
      </div>
      <ul className="m-0 flex list-none flex-wrap gap-1.5 p-0" aria-label="Readiness">
        {READINESS_CHIPS.map(({ key, label }) => {
          const entry = readiness?.[key];
          const tone = chipTone(entry);
          const showsFix = (tone === "bad" || tone === "warn") && Boolean(entry?.fix);
          const text = `${label}: ${tone === "unknown" ? "not reported" : chipValue(key, entry?.value)}`;
          return (
            <li key={key}>
              <button
                type="button"
                data-chip={key}
                data-tone={tone}
                className={cn("rounded-full border px-2 py-0.5 text-xs", toneClass[tone])}
                title={showsFix ? entry?.fix ?? undefined : entry?.detail}
                aria-expanded={showsFix ? openChip === key : undefined}
                onClick={() => {
                  if (showsFix) setOpenChip(openChip === key ? null : key);
                }}
              >
                {text}
              </button>
            </li>
          );
        })}
      </ul>
      {openEntry?.fix && (
        <p role="note" className="m-0 text-xs text-fg-dim">
          {openEntry.detail}. {openEntry.fix}.
        </p>
      )}
    </section>
  );
}

function Separator() {
  return (
    <span aria-hidden="true" className="text-fg-faint">
      ·
    </span>
  );
}

function RunningCalibration({
  status,
  onNavigate,
}: {
  status: StatusResponse | null;
  onNavigate: (item: WorkbenchItem) => void;
}) {
  const ecuId = status?.ecu_calibration_id ?? null;
  const matches = status?.calibration_matches_registry ?? null;
  const revision = status?.registry_revision ?? null;
  if (!ecuId) {
    return <span className="text-fg-dim">running: unknown (ECM not yet read)</span>;
  }
  if (matches === false) {
    return (
      <span className="inline-flex items-center gap-1.5">
        <span className="text-bad">running: {ecuId} — not in flash history</span>
        <button
          type="button"
          className="rounded border border-bad/60 px-1.5 text-xs text-bad"
          onClick={() => onNavigate("tuning-calibration")}
        >
          Calibration
        </button>
      </span>
    );
  }
  const changed = status?.ecu_calibration_changed === true;
  return (
    <span className="inline-flex items-center gap-1.5" title={`ECU reports ${ecuId}`}>
      <span>running {matches === true && revision ? revision : ecuId}</span>
      {matches === null && (
        <span className="text-xs text-warn">(flash history records no id)</span>
      )}
      {changed && (
        <button
          type="button"
          className="rounded border border-warn/60 px-1.5 text-xs text-warn"
          onClick={() => onNavigate("tuning-calibration")}
        >
          changed this session
        </button>
      )}
    </span>
  );
}
