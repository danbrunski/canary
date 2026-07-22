import { useCallback, useState } from "react";

import { getPowerStatus, setKeepAlive } from "../api/http";
import { Notice } from "../components/Notice";
import { Panel } from "../components/Panel";
import { ResourceGate } from "../components/ResourceGate";
import { useAsyncAction } from "../hooks/useAsyncAction";
import { useControlResource } from "../hooks/useControlResource";
import {
  buttonClass,
  cn,
  definitionListClass,
  definitionRowClass,
  definitionTermClass,
  definitionValueClass,
  inputClass,
  placeholderClass,
} from "../styles";
import type { PowerSnapshot } from "../types";
import { type Tone, toneClass } from "../util/format";
import { formatDuration } from "../util/time";

interface PowerPanelProps {
  token: string;
}

const formatPct = (v: number | null): string => (v == null ? "—" : `${v.toFixed(1)}%`);
const formatVolts = (v: number | null): string => (v == null ? "—" : `${v.toFixed(2)} V`);

/** Watchdog not running or its heartbeat gone stale is the operator-actionable
 *  state (no shutdown protection); on-battery is a warn (counting toward a
 *  shutdown); everything nominal is ok. */
function watchdogTone(p: PowerSnapshot): Tone | undefined {
  if (!p.watchdog_running || p.watchdog_stale) return "bad";
  if (p.on_battery) return "warn";
  return "ok";
}

function watchdogLabel(p: PowerSnapshot): string {
  if (!p.watchdog_running) return "not running";
  if (p.watchdog_stale) return "stale";
  if (p.on_battery) return "on battery";
  if (p.on_battery === false) return "external power";
  return "armed";
}

/** Mirrors the watchdog's own three-way on_battery (see
 *  ups_shutdown_watch.on_battery) — "external" covers both bench PSU and
 *  vehicle 12V alike, since the gauge can't tell those apart (only whether
 *  the battery is discharging). */
function powerSourceLabel(p: PowerSnapshot): string {
  if (!p.watchdog_running) return "—";
  if (p.on_battery === true) return "battery (input lost)";
  if (p.on_battery === false) return "external";
  return "unknown (no gauge data yet)";
}

/** Vehicle profiles is the reference panel for this shape: token-gated
 *  polling read + a single async write action, both via the shared hooks. */
export function PowerPanel({ token }: PowerPanelProps) {
  const fetcher = useCallback((signal: AbortSignal) => getPowerStatus(token, signal), [token]);
  const { data, error, loading, refresh } = useControlResource(fetcher, {
    enabled: Boolean(token),
    intervalMs: 5000,
  });

  const [minutes, setMinutes] = useState("15");
  const { notice, busy, run } = useAsyncAction();

  const onKeepAlive = () =>
    run("keep alive", async () => {
      const parsed = Number(minutes);
      if (!Number.isFinite(parsed) || parsed <= 0) {
        throw new Error("Enter a positive number of minutes.");
      }
      await setKeepAlive(token, parsed);
      refresh();
    });

  const onCancelHold = () =>
    run(
      "cancel hold",
      async () => {
        await setKeepAlive(token, 0);
        refresh();
      },
      { okText: "Keep-alive hold cancelled." },
    );

  return (
    <Panel title="Power / UPS">
      <ResourceGate
        enabled={Boolean(token)}
        enabledMessage="Enter the control token (Control panel) to view UPS status."
        loading={loading}
        loadingMessage="loading power status…"
        error={error}
        data={data}
      >
        {(data) => {
          const power = data.power;
          const holdActive = power.keepalive_remaining_s > 0;
          return (
            <>
              <Notice notice={notice} className="mb-2" />
              {!power.watchdog_running && (
                <p className={cn(placeholderClass, "mb-3")}>
                  The <code>ups-shutdown</code> watchdog isn't running (or hasn't written its
                  first heartbeat). No auto-shutdown protection is active — this is expected on a
                  bench Pi without the UPS wired up.
                </p>
              )}
              <dl className={definitionListClass}>
                <div className={definitionRowClass}>
                  <dt className={definitionTermClass}>Watchdog</dt>
                  <dd className={cn(definitionValueClass, toneClass(watchdogTone(power)))}>
                    {watchdogLabel(power)}
                  </dd>
                </div>
                <div className={definitionRowClass}>
                  <dt className={definitionTermClass}>Power source</dt>
                  <dd className={definitionValueClass}>{powerSourceLabel(power)}</dd>
                </div>
                <div className={definitionRowClass}>
                  <dt className={definitionTermClass}>Battery SoC</dt>
                  <dd className={definitionValueClass}>{formatPct(power.soc)}</dd>
                </div>
                <div className={definitionRowClass}>
                  <dt className={definitionTermClass}>Cell voltage</dt>
                  <dd className={definitionValueClass}>{formatVolts(power.vcell)}</dd>
                </div>
                {power.on_battery && (
                  <div className={definitionRowClass}>
                    <dt className={definitionTermClass}>Shutdown in</dt>
                    <dd className={cn(definitionValueClass, toneClass("warn"))}>
                      {formatDuration(
                        power.grace_s != null && power.seconds_lost != null
                          ? power.grace_s - power.seconds_lost
                          : null,
                        { collapseZero: true },
                      )}
                    </dd>
                  </div>
                )}
                <div className={definitionRowClass}>
                  <dt className={definitionTermClass}>Keep-alive hold</dt>
                  <dd className={cn(definitionValueClass, holdActive && toneClass("ok"))}>
                    {holdActive
                      ? formatDuration(power.keepalive_remaining_s, { collapseZero: true })
                      : "not set"}
                  </dd>
                </div>
              </dl>

              <div className="mt-3 flex items-center gap-2">
                <input
                  type="number"
                  min={1}
                  max={720}
                  className={cn(inputClass, "w-24")}
                  value={minutes}
                  aria-label="Keep-alive minutes"
                  onChange={(e) => setMinutes(e.target.value)}
                  disabled={busy !== null}
                />
                <button
                  type="button"
                  className={cn(buttonClass, "text-xs")}
                  onClick={onKeepAlive}
                  disabled={busy !== null}
                >
                  Keep alive
                </button>
                {holdActive && (
                  <button
                    type="button"
                    className={cn(buttonClass, "text-xs")}
                    onClick={onCancelHold}
                    disabled={busy !== null}
                  >
                    Cancel hold
                  </button>
                )}
              </div>
              <p className={cn(placeholderClass, "mt-2 text-[0.72rem]")}>
                Before a bench move longer than the watchdog's grace window (
                {formatDuration(power.grace_s, { collapseZero: true })}
                ), set a keep-alive hold so it doesn't power off mid-transition.
              </p>
            </>
          );
        }}
      </ResourceGate>
    </Panel>
  );
}
