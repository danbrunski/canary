import { useCallback, useState } from "react";

import { getSessionDigest, getTripSummary } from "../api/http";
import { Panel } from "../components/Panel";
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import { useControlResource } from "../hooks/useControlResource";
import { useNow } from "../hooks/useNow";
import {
  cn,
  definitionListClass,
  definitionRowClass,
  definitionTermClass,
  definitionValueClass,
  placeholderClass,
} from "../styles";
import type { SessionDigestResponse, StatusResponse, TripSummaryFields } from "../types";
import { formatPercent, KM_PER_MILE } from "../util/format";
import { numberOrNull, scalarText } from "../util/json";
import { formatDuration } from "../util/time";

interface TripPanelProps {
  channels: Map<string, ChannelSnapshot>;
  status: StatusResponse | null;
  token: string;
}

// Mirrors src/signals/derived.py's own unit constants exactly, so a
// display-only km->mi / L->gal / L-per-100km->mpg conversion here can never
// drift from what the backend prints in the last-drive summary.
const MI_PER_KM = 0.62137119;
const GAL_PER_L = 0.26417205;

function channelValue(channels: Map<string, ChannelSnapshot>, key: string): number | null {
  return numberOrNull(channels.get(key)?.latest.value);
}

function formatDistance(km: number | null): string {
  return km == null ? "—" : `${km.toFixed(1)} km (${(km / KM_PER_MILE).toFixed(1)} mi)`;
}

function formatFuel(l: number | null): string {
  return l == null ? "—" : `${l.toFixed(2)} L (${(l * GAL_PER_L).toFixed(2)} gal)`;
}

function formatEconomy(l100km: number | null, mpg?: number | null): string {
  if (l100km == null) return "—";
  return `${l100km.toFixed(1)} L/100km${mpg != null ? ` (${mpg.toFixed(1)} mpg)` : ""}`;
}

function formatCo2(kg: number | null): string {
  return kg == null ? "—" : `${kg.toFixed(2)} kg`;
}

function formatSpeed(kph: number | null): string {
  return kph == null ? "—" : `${kph.toFixed(0)} kph (${(kph / KM_PER_MILE).toFixed(0)} mph)`;
}

/** Current-drive economy/fuel/CO2/idle stats (Task 1.6) — reads the same
 *  `derived:*` channels already streaming over `/ws/telemetry` (Task 1.1-1.3),
 *  the exact accumulators `LiveFeatureSource` maintains, not a re-fetch or a
 *  client-side recompute of the integrals themselves. Plus the last stored
 *  drive's summary from `GET /sessions/{id}/trip-summary` (Task 1.5). */
export function TripPanel({ channels, status, token }: TripPanelProps) {
  const distanceKm = channelValue(channels, "derived:TRIP_DISTANCE_KM");
  const fuelUsedL = channelValue(channels, "derived:FUEL_USED_L");
  const co2Kg = channelValue(channels, "derived:CO2_KG");
  const idleTimeS = channelValue(channels, "derived:IDLE_TIME_S");
  const avgEconomyL100 = channelValue(channels, "derived:AVG_ECONOMY_L_PER_100KM");
  const avgEconomyMpg =
    distanceKm != null && distanceKm > 0 && fuelUsedL != null && fuelUsedL > 0
      ? (distanceKm * MI_PER_KM) / (fuelUsedL * GAL_PER_L)
      : null;

  // Idle fraction needs a trip duration, which isn't one of the streamed
  // accumulators (only the running totals are). We don't have "since the
  // session started" client-side, so this is "since this panel has been
  // open" — an honest, if partial, denominator rather than a fabricated
  // one. The last-drive summary below carries the precise backend-computed
  // idle_fraction instead.
  const [openedAt] = useState(() => Date.now());
  const now = useNow();
  const observedSeconds = (now - openedAt) / 1000;
  const idleFraction =
    idleTimeS != null && observedSeconds > 1 ? Math.min(idleTimeS / observedSeconds, 1) : null;

  const hasLiveData = distanceKm != null || fuelUsedL != null || idleTimeS != null;

  const sessionId = status?.latest_session ?? null;
  const fetcher = useCallback(
    (signal: AbortSignal) => {
      if (!sessionId) return Promise.reject(new Error("no session"));
      return getTripSummary(sessionId, signal);
    },
    [sessionId],
  );
  const { data, error, loading } = useControlResource(fetcher, {
    enabled: Boolean(sessionId),
    intervalMs: 30000,
  });

  // Digest requires a control token (auth-consistency follow-up to Task
  // 5.6 — it aggregates the vehicle ledger + fingerprint z-scores, both
  // otherwise only readable via authenticated /control/* routes) — don't
  // fetch, and don't render a broken/erroring section, until one's entered.
  const digestFetcher = useCallback(
    (signal: AbortSignal) => {
      if (!sessionId) return Promise.reject(new Error("no session"));
      return getSessionDigest(token, sessionId, signal);
    },
    [sessionId, token],
  );
  const digest = useControlResource(digestFetcher, {
    enabled: Boolean(sessionId) && Boolean(token),
    intervalMs: 30000,
  });

  return (
    <Panel title="Trip">
      <h3 className="mb-1 text-[0.66rem] uppercase tracking-[0.12em] text-fg-faint">
        Current drive
      </h3>
      {!hasLiveData ? (
        <p className={placeholderClass}>no live drive data yet</p>
      ) : (
        <dl className={cn(definitionListClass, "mb-4")}>
          <div className={definitionRowClass}>
            <dt className={definitionTermClass}>distance</dt>
            <dd className={definitionValueClass}>{formatDistance(distanceKm)}</dd>
          </div>
          <div className={definitionRowClass}>
            <dt className={definitionTermClass}>fuel used</dt>
            <dd className={definitionValueClass}>{formatFuel(fuelUsedL)}</dd>
          </div>
          <div className={definitionRowClass}>
            <dt className={definitionTermClass}>avg economy</dt>
            <dd className={definitionValueClass}>{formatEconomy(avgEconomyL100, avgEconomyMpg)}</dd>
          </div>
          <div className={definitionRowClass}>
            <dt className={definitionTermClass}>CO2</dt>
            <dd className={definitionValueClass}>{formatCo2(co2Kg)}</dd>
          </div>
          <div className={definitionRowClass}>
            <dt className={definitionTermClass}>idle time</dt>
            <dd className={definitionValueClass}>{formatDuration(idleTimeS)}</dd>
          </div>
          <div className={definitionRowClass}>
            <dt className={definitionTermClass}>idle fraction</dt>
            <dd className={definitionValueClass}>{formatPercent(idleFraction, 0)}</dd>
          </div>
        </dl>
      )}

      <h3 className="mb-1 text-[0.66rem] uppercase tracking-[0.12em] text-fg-faint">Last drive</h3>
      {!sessionId ? (
        <p className={placeholderClass}>no recorded session yet</p>
      ) : loading ? (
        <p className={placeholderClass}>loading trip summary…</p>
      ) : error || !data ? (
        <p className={cn(placeholderClass, "text-bad")}>{error ?? "no data"}</p>
      ) : (
        <LastDriveSummary sessionId={data.session_id} summary={data.summary} />
      )}

      {sessionId && (
        <>
          <h3 className="mb-1 mt-4 text-[0.66rem] uppercase tracking-[0.12em] text-fg-faint">
            Last drive digest
          </h3>
          {!token ? (
            <p className={placeholderClass}>
              Enter the control token (Control panel) to view the last-drive digest.
            </p>
          ) : digest.loading ? (
            <p className={placeholderClass}>loading digest…</p>
          ) : digest.error || !digest.data ? (
            <p className={cn(placeholderClass, "text-bad")}>{digest.error ?? "no data"}</p>
          ) : (
            <DigestSummary digest={digest.data} />
          )}
        </>
      )}
    </Panel>
  );
}

function countsSummary(counts: Record<string, number>): { total: number; bySource: string } {
  return {
    total: Object.values(counts).reduce((sum, count) => sum + count, 0),
    bySource: Object.entries(counts)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([source, count]) => `${source}=${count}`)
      .join(", "),
  };
}

function DigestSummary({ digest }: { digest: SessionDigestResponse }) {
  const { total: totalAnomalies, bySource } = countsSummary(digest.anomaly_counts_by_source);
  // A06.5: evidence-role detector events are not alerts — their own row,
  // never added to the gated count; absent on schema-1 digests.
  const evidence = countsSummary(digest.evidence_counts_by_source ?? {});
  const topMover = digest.fingerprint_top_movers[0];

  return (
    <dl className={definitionListClass}>
      <div className={definitionRowClass}>
        <dt className={definitionTermClass}>gated anomalies</dt>
        <dd className={definitionValueClass}>
          {totalAnomalies}
          {bySource && <span className="text-fg-dim"> ({bySource})</span>}
        </dd>
      </div>
      {evidence.total > 0 && (
        <div className={definitionRowClass}>
          <dt className={definitionTermClass}>evidence (not alerts)</dt>
          <dd className={definitionValueClass}>
            {evidence.total}
            <span className="text-fg-dim"> ({evidence.bySource})</span>
          </dd>
        </div>
      )}
      <div className={definitionRowClass}>
        <dt className={definitionTermClass}>new DTCs</dt>
        <dd className={definitionValueClass}>
          {digest.new_dtcs.length === 0
            ? "none"
            : digest.new_dtcs.map((entry) => scalarText(entry.code) ?? "?").join(", ")}
        </dd>
      </div>
      <div className={definitionRowClass}>
        <dt className={definitionTermClass}>new ledger events</dt>
        <dd className={definitionValueClass}>{digest.new_ledger_events.length}</dd>
      </div>
      {topMover && (
        <div className={definitionRowClass}>
          <dt className={definitionTermClass}>top mover</dt>
          <dd className={definitionValueClass}>
            {topMover.stat}
            {topMover.z_score != null && (
              <span className="text-fg-dim"> (z={topMover.z_score.toFixed(2)})</span>
            )}
          </dd>
        </div>
      )}
    </dl>
  );
}

function LastDriveSummary({
  sessionId,
  summary,
}: {
  sessionId: string;
  summary: TripSummaryFields;
}) {
  return (
    <dl className={definitionListClass}>
      <div className={definitionRowClass}>
        <dt className={definitionTermClass}>session</dt>
        <dd className={definitionValueClass}>{sessionId}</dd>
      </div>
      <div className={definitionRowClass}>
        <dt className={definitionTermClass}>duration</dt>
        <dd className={definitionValueClass}>{formatDuration(summary.duration_s)}</dd>
      </div>
      <div className={definitionRowClass}>
        <dt className={definitionTermClass}>distance</dt>
        <dd className={definitionValueClass}>{formatDistance(summary.distance_km)}</dd>
      </div>
      <div className={definitionRowClass}>
        <dt className={definitionTermClass}>fuel used</dt>
        <dd className={definitionValueClass}>{formatFuel(summary.fuel_used_l)}</dd>
      </div>
      <div className={definitionRowClass}>
        <dt className={definitionTermClass}>avg economy</dt>
        <dd className={definitionValueClass}>
          {formatEconomy(summary.avg_economy_l_per_100km, summary.avg_economy_mpg)}
        </dd>
      </div>
      <div className={definitionRowClass}>
        <dt className={definitionTermClass}>CO2</dt>
        <dd className={definitionValueClass}>{formatCo2(summary.co2_kg)}</dd>
      </div>
      <div className={definitionRowClass}>
        <dt className={definitionTermClass}>idle</dt>
        <dd className={definitionValueClass}>
          {formatDuration(summary.idle_time_s)}
          {summary.idle_fraction != null && (
            <span className="text-fg-dim"> ({formatPercent(summary.idle_fraction, 0)})</span>
          )}
        </dd>
      </div>
      <div className={definitionRowClass}>
        <dt className={definitionTermClass}>max / avg speed</dt>
        <dd className={definitionValueClass}>
          {formatSpeed(summary.max_speed_kph)} / {formatSpeed(summary.avg_speed_kph)}
        </dd>
      </div>
      {(summary.zero_to_60_mph_s != null || summary.quarter_mile_s != null) && (
        <div className={definitionRowClass}>
          <dt className={definitionTermClass}>0-60 / 1/4 mile</dt>
          <dd className={definitionValueClass}>
            {summary.zero_to_60_mph_s != null ? `${summary.zero_to_60_mph_s.toFixed(2)}s` : "—"}
            {" / "}
            {summary.quarter_mile_s != null
              ? `${summary.quarter_mile_s.toFixed(2)}s @ ${formatSpeed(summary.quarter_mile_trap_kph)}`
              : "—"}
          </dd>
        </div>
      )}
    </dl>
  );
}
