import { Panel } from "../components/Panel";
import { useLiveFullStatus } from "../hooks/statusDemand";
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import {
  cn,
  definitionListClass,
  definitionRowClass,
  definitionTermClass,
  definitionValueClass,
  placeholderClass,
} from "../styles";
import type { GpsStatus, ImuStatus, PowerStatus, StatusResponse, WorkbenchItem } from "../types";
import { STALE_THRESHOLD_MS, computeFreshness } from "../util/freshness";
import {
  type Tone,
  formatAltitude,
  formatCoords,
  formatGpsSpeed,
  formatHeading,
  formatPercent,
  formatTemp,
  toneClass,
} from "../util/format";

interface SystemPanelProps {
  status: StatusResponse | null;
  channels: Map<string, ChannelSnapshot>;
  statusError: Error | null;
  /** Jump to another Workbench panel — used by the "Trip" pointer row
   *  (Task 1.6) since this panel no longer renders trip stats itself. */
  onNavigate?: (item: WorkbenchItem) => void;
}

/** Pi soft-throttles around 80 °C; warn before then. */
function tempTone(c: number | null | undefined): Tone | undefined {
  if (c == null) return undefined;
  if (c > 80) return "bad";
  if (c > 70) return "warn";
  return undefined;
}

/** CPU/memory pressure: warn past 85 % utilization. */
function loadTone(fraction: number | null | undefined): Tone | undefined {
  return fraction != null && fraction > 0.85 ? "warn" : undefined;
}

const POWER_EVENT_LABELS: Record<string, string> = {
  under_voltage: "under-voltage",
  arm_frequency_capped: "freq capped",
  throttling: "throttled",
  soft_temp_limit: "temp limit",
};

/** Live power state → label + tone. Under-voltage or active throttling is a
 *  power-supply fault (bad); a frequency cap / soft-temp-limit is a warn. */
function currentPower(p: PowerStatus | null | undefined): {
  label: string;
  tone: Tone | undefined;
} {
  if (p == null) return { label: "—", tone: undefined };
  if (p.now.under_voltage) return { label: "under-voltage", tone: "bad" };
  if (p.now.currently_throttled) return { label: "throttled", tone: "bad" };
  if (p.now.arm_frequency_capped) return { label: "freq capped", tone: "warn" };
  if (p.now.soft_temp_limit) return { label: "temp-limited", tone: "warn" };
  return { label: "ok", tone: "ok" };
}

/** Sticky "has occurred since boot" flags — the past-detected anomalies. */
function pastPowerEvents(p: PowerStatus | null | undefined): string[] {
  if (p == null) return [];
  return Object.entries(p.since_boot)
    .filter(([, occurred]) => occurred)
    .map(([key]) => POWER_EVENT_LABELS[key] ?? key);
}

/** GPS fix → label + tone. A current fix is good; no/stale fix is a warn (the
 *  GPS is optional, so its absence is informational, never an error). */
function gpsFix(g: GpsStatus): { label: string; tone: Tone | undefined } {
  if (g.has_fix) return { label: `fix · ${g.num_satellites} sats`, tone: "ok" };
  const acquiring = g.num_satellites > 0 ? `acquiring · ${g.num_satellites} sats` : "no fix";
  return { label: acquiring, tone: "warn" };
}

/** IMU collector → label + tone (H02.5). Like GPS, the IMU is optional, so a
 *  missing or stale stream warns rather than errs. FIFO overruns and queue
 *  drops are lost samples, so any since start also warns. */
export function imuHealth(imu: ImuStatus): { label: string; tone: Tone | undefined } {
  if (imu.age_seconds == null) return { label: "no samples", tone: "warn" };
  if (!imu.fresh) return { label: `stale · ${imu.age_seconds.toFixed(1)} s`, tone: "warn" };
  const configured = imu.configured_hz != null ? ` / ${imu.configured_hz.toFixed(0)}` : "";
  const label =
    imu.achieved_hz == null ? "rate —" : `${imu.achieved_hz.toFixed(1)}${configured} Hz`;
  return { label, tone: imu.overruns + imu.queue_dropped > 0 ? "warn" : "ok" };
}

/** System health: storage, host vitals (Pi temp/CPU/memory), latest
 *  session, vehicle line, data freshness.
 *
 *  Host vitals come from `/status.health.system` (mirrors
 *  `HealthMonitor.snapshot()`); each reads "—" until the Pi reports it
 *  (cpu_percent needs a second poll for its baseline, temp is null off-Pi).
 *
 *  Stale-data detection uses the newest timestamp seen across all
 *  channels (shared `computeFreshness`) — if it's more than 10 s old,
 *  surface "stale" so the operator knows the daemon stopped emitting
 *  (vs. just "no anomalies"). */
export function SystemPanel({ status, channels, statusError, onNavigate }: SystemPanelProps) {
  // GPS, IMU, power and the detector blind modes are in the full `/status`.
  useLiveFullStatus();
  const freshness = computeFreshness(channels);
  const stale = freshness !== null && freshness > STALE_THRESHOLD_MS;
  const system = status?.health?.system ?? null;
  const power = status?.health?.power ?? null;
  const powerNow = currentPower(power);
  const powerEvents = pastPowerEvents(power);
  const gps = status?.gps ?? null;
  const gpsFixState = gps ? gpsFix(gps) : null;
  const imu = status?.imu ?? null;
  const imuState = imu ? imuHealth(imu) : null;
  const blindModes = status?.detectors?.isolation_forest.blind_modes ?? [];
  const can0 = status?.health?.can0 ?? null;
  const can0Growth = can0 ? can0.since_start.rx_over_errors + can0.since_start.rx_dropped : 0;

  return (
    <Panel title="System">
      {statusError && (
        <div className="mb-2 rounded-md bg-bad/12 px-2 py-1.5 text-[0.82rem] text-bad">
          /status unreachable: {statusError.message}
        </div>
      )}
      {!status ? (
        <p className={placeholderClass}>loading…</p>
      ) : (
        <dl className={definitionListClass}>
          <div className={definitionRowClass}>
            <dt className={definitionTermClass}>vehicle</dt>
            <dd className={definitionValueClass}>{status.vehicle || "unknown"}</dd>
          </div>
          <div className={definitionRowClass}>
            <dt className={definitionTermClass}>storage</dt>
            <dd className={definitionValueClass}>
              {status.free_mb.toFixed(0)} MB free{" "}
              <span className="text-fg-dim">at {status.storage_path}</span>
            </dd>
          </div>
          <div className={definitionRowClass}>
            <dt className={definitionTermClass}>cpu temp</dt>
            <dd className={cn(definitionValueClass, toneClass(tempTone(system?.cpu_temp_c)))}>
              {formatTemp(system?.cpu_temp_c)}
            </dd>
          </div>
          <div className={definitionRowClass}>
            <dt className={definitionTermClass}>cpu usage</dt>
            <dd className={cn(definitionValueClass, toneClass(loadTone(system?.cpu_percent)))}>
              {formatPercent(system?.cpu_percent, 0)}
            </dd>
          </div>
          <div className={definitionRowClass}>
            <dt className={definitionTermClass}>memory</dt>
            <dd
              className={cn(definitionValueClass, toneClass(loadTone(system?.mem_used_fraction)))}
            >
              {formatPercent(system?.mem_used_fraction, 0)}
              {system?.mem_total_mb != null && (
                <span className="text-fg-dim"> of {system.mem_total_mb.toFixed(0)} MB</span>
              )}
            </dd>
          </div>
          <div className={definitionRowClass}>
            <dt className={definitionTermClass}>power</dt>
            <dd className={cn(definitionValueClass, toneClass(powerNow.tone))}>{powerNow.label}</dd>
          </div>
          <div className={definitionRowClass}>
            <dt className={definitionTermClass}>power events</dt>
            <dd className={cn(definitionValueClass, powerEvents.length > 0 && "text-warn")}>
              {power == null ? "—" : powerEvents.length === 0 ? "none" : powerEvents.join(", ")}
              {powerEvents.length > 0 && <span className="text-fg-dim"> (since boot)</span>}
            </dd>
          </div>
          {status.detectors && (
            <div className={definitionRowClass}>
              <dt className={definitionTermClass}>IF blind modes</dt>
              <dd className={cn(definitionValueClass, blindModes.length > 0 && "text-warn")}>
                {blindModes.length === 0 ? "none" : blindModes.join(", ")}
              </dd>
            </div>
          )}
          {can0 && (
            <div className={definitionRowClass}>
              <dt className={definitionTermClass}>can0 rx overruns</dt>
              <dd className={cn(definitionValueClass, can0Growth > 0 && "text-warn")}>
                {can0.rx_over_errors} overrun / {can0.rx_dropped} dropped
                <span className="text-fg-dim">
                  {" "}
                  (+{can0.since_start.rx_over_errors} / +{can0.since_start.rx_dropped} since start)
                </span>
              </dd>
            </div>
          )}
          {imu && imuState && (
            <div className={definitionRowClass}>
              <dt className={definitionTermClass}>imu</dt>
              <dd className={cn(definitionValueClass, toneClass(imuState.tone))}>
                {imuState.label}
                <span className="text-fg-dim">
                  {" "}
                  · {imu.overruns} overrun / {imu.queue_dropped} dropped
                </span>
              </dd>
            </div>
          )}
          {gps && gpsFixState && (
            <>
              <div className={definitionRowClass}>
                <dt className={definitionTermClass}>gps</dt>
                <dd className={cn(definitionValueClass, toneClass(gpsFixState.tone))}>
                  {gpsFixState.label}
                  {gps.hdop != null && gps.has_fix && (
                    <span className="text-fg-dim"> · hdop {gps.hdop.toFixed(1)}</span>
                  )}
                </dd>
              </div>
              <div className={definitionRowClass}>
                <dt className={definitionTermClass}>location</dt>
                <dd className={definitionValueClass}>
                  {formatCoords(gps.latitude, gps.longitude)}
                </dd>
              </div>
              <div className={definitionRowClass}>
                <dt className={definitionTermClass}>altitude</dt>
                <dd className={definitionValueClass}>{formatAltitude(gps.altitude)}</dd>
              </div>
              <div className={definitionRowClass}>
                <dt className={definitionTermClass}>gps speed</dt>
                <dd className={definitionValueClass}>{formatGpsSpeed(gps.speed)}</dd>
              </div>
              <div className={definitionRowClass}>
                <dt className={definitionTermClass}>heading</dt>
                <dd className={definitionValueClass}>{formatHeading(gps.heading)}</dd>
              </div>
            </>
          )}
          <div className={definitionRowClass}>
            <dt className={definitionTermClass}>trip</dt>
            <dd className={definitionValueClass}>
              {onNavigate ? (
                <button
                  type="button"
                  className="text-accent underline decoration-dotted underline-offset-2 hover:no-underline"
                  onClick={() => onNavigate("observe-trip")}
                >
                  see Trip panel →
                </button>
              ) : (
                "see Trip panel (Live ▸ Trip)"
              )}
            </dd>
          </div>
          <div className={definitionRowClass}>
            <dt className={definitionTermClass}>latest session</dt>
            <dd className={definitionValueClass}>{status.latest_session ?? "none yet"}</dd>
          </div>
          <div className={definitionRowClass}>
            <dt className={definitionTermClass}>data freshness</dt>
            <dd className={cn(definitionValueClass, stale && "text-bad")}>
              {freshness === null ? "—" : `${(freshness / 1000).toFixed(1)}s`}
              {stale && " (stale)"}
            </dd>
          </div>
        </dl>
      )}
    </Panel>
  );
}
