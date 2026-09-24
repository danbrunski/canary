import { useCallback } from "react";

import { getCurrentCapture } from "../../api/http";
import { useControlResource } from "../../hooks/useControlResource";
import { useTagQueue } from "../../hooks/useTagQueue";
import { buttonClass, panelClass } from "../../styles";
import type { ChannelSnapshot } from "../../hooks/useTelemetryStream";
import type { StatusResponse, WorkbenchItem } from "../../types";
import { CaptureProfilesPanel } from "../CaptureProfilesPanel";
import { PowerPanel } from "../PowerPanel";

// Pull labels are the backend's canonical ones (src/analysis/pull_tags.py);
// tests/test_pull_tags.py fails if these drift from what the digest counts.
const PHYSICAL_TAGS = [
  ["tuning_pull_start", "Pull start"],
  ["tuning_pull_end", "Pull end"],
  ["steady_hold_start", "Steady hold start"],
  ["steady_hold_end", "Steady hold end"],
  ["decel", "Decel"],
  ["idle", "Idle"],
] as const;

export function LoggingPage({
  token,
  status,
  channels,
  onNavigate,
}: {
  token: string;
  status: StatusResponse | null;
  channels: Map<string, ChannelSnapshot>;
  onNavigate: (item: WorkbenchItem) => void;
}) {
  const fetchCapture = useCallback(
    (signal: AbortSignal) => getCurrentCapture(token, signal),
    [token],
  );
  const captureResource = useControlResource(fetchCapture, {
    enabled: Boolean(token),
    intervalMs: 2500,
  });
  const capture = captureResource.data?.capture ?? null;
  const tags = useTagQueue(token, capture);

  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-fg-dim">
        CANary records default telemetry continuously. Starting a log switches to the selected
        profile and opens a separate capture. Stop closes that log and restores default collection.
      </p>
      <CaptureProfilesPanel
        token={token}
        status={status}
        channels={channels}
        authoring={false}
        showRunner
      />
      {status?.vehicle_state?.state === "ignition_only" && (
        <p className="text-sm text-fg-dim">
          Key on, engine off: diagnostic channels can answer, but engine values will not move.
        </p>
      )}
      {status?.storage_warning_mb != null && status.free_mb < status.storage_warning_mb && (
        <p className="text-sm text-warn">
          Storage is below the {status.storage_warning_mb.toFixed(0)} MB warning floor
          ({" "}{status.free_mb.toFixed(0)} MB free).
        </p>
      )}

      <section className={panelClass}>
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="mr-auto text-sm font-semibold">Physical action tags</h2>
          <span className="text-xs text-fg-dim">
            {capture ? `${tags.pendingCount} queued` : "start a capture to tag"}
          </span>
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          {PHYSICAL_TAGS.map(([label, title]) => (
            <button
              key={label}
              type="button"
              className={buttonClass}
              disabled={!capture}
              onClick={() => tags.enqueue(label, "physical_action")}
            >
              {title}
              {(tags.tapCounts.get(label) ?? 0) > 0 && ` (${tags.tapCounts.get(label)})`}
            </button>
          ))}
        </div>
        {captureResource.error && (
          <p className="mt-2 text-xs text-warn">Tag status: {captureResource.error}</p>
        )}
      </section>

      <PowerPanel token={token} />

      <div className="flex flex-wrap gap-2">
        <button type="button" className={buttonClass} onClick={() => onNavigate("tuning-logs")}>View logs</button>
        <button type="button" className={buttonClass} onClick={() => onNavigate("system-capture-profiles")}>Edit profiles in System</button>
      </div>
    </div>
  );
}
