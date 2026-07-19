import { Panel } from "../components/Panel";
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import {
  cn,
  definitionListClass,
  definitionRowClass,
  definitionTermClass,
  definitionValueClass,
  placeholderClass,
} from "../styles";
import type { GpsStatus, PowerStatus, StatusResponse, TripStats } from "../types";
import { STALE_THRESHOLD_MS, computeFreshness } from "../util/freshness";
import { type Tone, toneClass } from "../util/format";

const KM_PER_MILE = 1.609344;
const M_PER_FT = 0.3048;
const CARDINALS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];

interface SystemPanelProps {
  status: StatusResponse | null;
  channels: Map<string, ChannelSnapshot>;
  statusError: Error | null;
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

const formatTemp = (c: number | null | undefined): string =>
  c == null ? "—" : `${c.toFixed(1)} °C`;

const formatPct = (fraction: number | null | undefined): string =>
  fraction == null ? "—" : `${(fraction * 100).toFixed(0)}%`;

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

const formatCoords = (g: GpsStatus): string =>
  g.latitude == null || g.longitude == null
    ? "—"
    : `${g.latitude.toFixed(5)}, ${g.longitude.toFixed(5)}`;

const formatAltitude = (m: number | null): string =>
  m == null ? "—" : `${(m / M_PER_FT).toFixed(0)} ft`;

/** GPS speed arrives in km/h; show mph for this US vehicle. */
const formatGpsSpeed = (kmh: number | null): string =>
  kmh == null ? "—" : `${(kmh / KM_PER_MILE).toFixed(0)} mph`;

const formatHeading = (deg: number | null): string =>
  deg == null ? "—" : `${deg.toFixed(0)}° ${CARDINALS[Math.round(deg / 45) % 8]}`;

const formatMiles = (t: TripStats): string =>
  `${t.distance_miles.toFixed(1)} mi`;

function formatDriveTime(seconds: number): string {
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m ${total % 60}s`;
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
export function SystemPanel({ status, channels, statusError }: SystemPanelProps) {
  const freshness = computeFreshness(channels);
  const stale = freshness !== null && freshness > STALE_THRESHOLD_MS;
  const system = status?.health?.system ?? null;
  const power = status?.health?.power ?? null;
  const powerNow = currentPower(power);
  const powerEvents = pastPowerEvents(power);
  const gps = status?.gps ?? null;
  const trip = status?.trip ?? null;
  const gpsFixState = gps ? gpsFix(gps) : null;
  const blindModes = status?.detectors?.isolation_forest.blind_modes ?? [];

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
              {formatPct(system?.cpu_percent)}
            </dd>
          </div>
          <div className={definitionRowClass}>
            <dt className={definitionTermClass}>memory</dt>
            <dd className={cn(definitionValueClass, toneClass(loadTone(system?.mem_used_fraction)))}>
              {formatPct(system?.mem_used_fraction)}
              {system?.mem_total_mb != null && (
                <span className="text-fg-dim"> of {system.mem_total_mb.toFixed(0)} MB</span>
              )}
            </dd>
          </div>
          <div className={definitionRowClass}>
            <dt className={definitionTermClass}>power</dt>
            <dd className={cn(definitionValueClass, toneClass(powerNow.tone))}>
              {powerNow.label}
            </dd>
          </div>
          <div className={definitionRowClass}>
            <dt className={definitionTermClass}>power events</dt>
            <dd className={cn(definitionValueClass, powerEvents.length > 0 && "text-warn")}>
              {power == null
                ? "—"
                : powerEvents.length === 0
                  ? "none"
                  : powerEvents.join(", ")}
              {powerEvents.length > 0 && (
                <span className="text-fg-dim"> (since boot)</span>
              )}
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
                <dd className={definitionValueClass}>{formatCoords(gps)}</dd>
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
          {trip && (
            <>
              <div className={definitionRowClass}>
                <dt className={definitionTermClass}>trip distance</dt>
                <dd className={definitionValueClass}>
                  {formatMiles(trip)}
                  <span className="text-fg-dim"> ({trip.distance_km.toFixed(1)} km)</span>
                </dd>
              </div>
              <div className={definitionRowClass}>
                <dt className={definitionTermClass}>drive time</dt>
                <dd className={definitionValueClass}>
                  {formatDriveTime(trip.moving_time_seconds)}
                </dd>
              </div>
            </>
          )}
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
