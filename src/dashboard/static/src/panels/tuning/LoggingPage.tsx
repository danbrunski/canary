import { useCallback } from "react";

import { getCurrentCapture } from "../../api/http";
import { useControlResource } from "../../hooks/useControlResource";
import { useTagQueue } from "../../hooks/useTagQueue";
import { buttonClass, panelClass } from "../../styles";
import type { ChannelSnapshot } from "../../hooks/useTelemetryStream";
import type { StatusResponse, WorkbenchItem } from "../../types";
import { CaptureProfilesPanel } from "../CaptureProfilesPanel";
import { PowerPanel } from "../PowerPanel";

const PHYSICAL_TAGS = [
  ["pull_start", "Pull start"],
  ["pull_end", "Pull end"],
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
      <CaptureProfilesPanel
        token={token}
        status={status}
        channels={channels}
        authoring={false}
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

      <button
        type="button"
        className={`${buttonClass} self-start`}
        onClick={() => onNavigate("system-capture-profiles")}
      >
        Edit capture profiles in System
      </button>
    </div>
  );
}
