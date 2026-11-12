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
import type { PowerCountdownReason, PowerSnapshot } from "../types";
import { type Tone, toneClass } from "../util/format";
import { formatDuration } from "../util/time";

interface PowerPanelProps {
  token: string;
}

const formatPct = (v: number | null): string => (v == null ? "—" : `${v.toFixed(1)}%`);
const formatVolts = (v: number | null): string => (v == null ? "—" : `${v.toFixed(2)} V`);

const COUNTDOWN_REASON: Record<PowerCountdownReason, string> = {
  input_lost_discharging: "input lost, battery discharging",
  input_lost_soc_critical: "input lost, SoC at or below the critical floor",
  input_lost_unconfirmed: "input lost, battery state not confirmed",
};

/** The shutdown countdown is running (or would, under a keep-alive hold). A
 *  heartbeat from a watchdog older than D03.67 has no `shutdown_state`; its
 *  own `on_battery` stands in for it until it is redeployed. */
function countdownRunning(p: PowerSnapshot): boolean {
  if (p.shutdown_state != null) return p.shutdown_state === "counting_down";
  return p.on_battery === true;
}

/** Watchdog not running or its heartbeat gone stale is the operator-actionable
 *  state (no shutdown protection); a countdown (or one a keep-alive is
 *  holding) is a warn; everything nominal is ok. */
function watchdogTone(p: PowerSnapshot): Tone | undefined {
  if (!p.watchdog_running || p.watchdog_stale) return "bad";
  if (countdownRunning(p) || p.shutdown_state === "held_keepalive") return "warn";
  return "ok";
}

function watchdogLabel(p: PowerSnapshot): string {
  if (!p.watchdog_running) return "not running";
  if (p.watchdog_stale) return "stale";
  switch (p.shutdown_state) {
    case "disarmed":
      return "disarmed";
    case "counting_down":
      return "shutdown pending";
    case "held_keepalive":
      return "held by keep-alive";
    case "held_external_power":
    case "input_present":
      return "external power";
    default:
      break;
  }
  if (p.on_battery) return "on battery";
  if (p.on_battery === false) return "external power";
  return "armed";
}

/** The PLD line alone — never mixed with the battery's state. */
function inputLabel(p: PowerSnapshot): string {
  if (!p.watchdog_running || p.input_lost == null) return "—";
  return p.input_lost ? "lost" : "present";
}

/** The measured SoC trend, independent of the input line: it can fall with
 *  input present (a sagging supply), and rise while the SoC reads critical. */
function batteryLabel(p: PowerSnapshot): string {
  if (!p.watchdog_running) return "—";
  if (p.discharging == null) return "unknown (no gauge data yet)";
  const rate = p.discharge_rate_pct_per_hour;
  const rateText = rate == null ? "" : ` (${rate > 0 ? "+" : ""}${rate.toFixed(1)} %/h)`;
  if (p.discharging) return `discharging${rateText}`;
  if (rate != null && rate > 0) return `charging${rateText}`;
  return `steady${rateText}`;
}

function socLabel(p: PowerSnapshot): string {
  const base = formatPct(p.soc);
  if (!p.soc_critical) return base;
  const floor = p.critical_soc_pct == null ? "" : ` ${p.critical_soc_pct.toFixed(0)}%`;
  return `${base} — critical (at or below${floor})`;
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
        tokenPurpose="view UPS status"
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
                  The <code>ups-shutdown</code> watchdog isn't running (or hasn't written its first
                  heartbeat). No auto-shutdown protection is active — this is expected on a bench Pi
                  without the UPS wired up.
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
                  <dt className={definitionTermClass}>Input power</dt>
                  <dd className={definitionValueClass}>{inputLabel(power)}</dd>
                </div>
                <div className={definitionRowClass}>
                  <dt className={definitionTermClass}>Battery</dt>
                  <dd className={definitionValueClass}>{batteryLabel(power)}</dd>
                </div>
                <div className={definitionRowClass}>
                  <dt className={definitionTermClass}>Battery SoC</dt>
                  <dd className={cn(definitionValueClass, power.soc_critical && toneClass("warn"))}>
                    {socLabel(power)}
                  </dd>
                </div>
                <div className={definitionRowClass}>
                  <dt className={definitionTermClass}>Cell voltage</dt>
                  <dd className={definitionValueClass}>{formatVolts(power.vcell)}</dd>
                </div>
                {countdownRunning(power) && (
                  <div className={definitionRowClass}>
                    <dt className={definitionTermClass}>Shutdown in</dt>
                    <dd className={cn(definitionValueClass, toneClass("warn"))}>
                      {formatDuration(
                        power.grace_s != null && power.seconds_lost != null
                          ? power.grace_s - power.seconds_lost
                          : null,
                        { collapseZero: true },
                      )}
                      {power.countdown_reason && ` — ${COUNTDOWN_REASON[power.countdown_reason]}`}
                    </dd>
                  </div>
                )}
                {power.shutdown_state === "held_keepalive" && (
                  <div className={definitionRowClass}>
                    <dt className={definitionTermClass}>Shutdown</dt>
                    <dd className={cn(definitionValueClass, toneClass("warn"))}>
                      held by the keep-alive hold
                      {power.countdown_reason && ` (${COUNTDOWN_REASON[power.countdown_reason]})`}
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
