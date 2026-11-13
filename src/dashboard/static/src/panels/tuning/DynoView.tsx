import { useCallback, useMemo, useState } from "react";
import { TableScroll } from "../../components/TableScroll";

import { Panel } from "../../components/Panel";
import { SegmentedControl, type SegmentOption } from "../../components/SegmentedControl";
import { getDyno } from "../../api/workbench";
import { getProfiles } from "../../api/http";
import { useControlResource } from "../../hooks/useControlResource";
import { useAnalysisRun } from "../../hooks/useWorkbench";
import type {
  DynoCurve,
  DynoFloor,
  DynoPrerequisite,
  DynoQuantity,
  DynoResponse,
  DynoRunOk,
  DynoUnavailableKind,
  SessionChoice,
} from "../../types/workbench";
import { buttonClass, cn, inputClass, selectClass } from "../../styles";
import { toCsv } from "../../util/csv";
import { downloadText } from "../../util/browser";
import { kpaToPsi, readTirePsi } from "../../util/pressure";
import { ConditionsBlock, RefusalNotice, UnavailableList } from "./WorkbenchNotices";

const QUANTITIES: ReadonlyArray<SegmentOption<DynoQuantity>> = [
  { id: "wheel_power_kw", label: "Wheel power (kW)" },
  { id: "wheel_torque_nm", label: "Wheel torque (N m)" },
];

const UNIT: Record<DynoQuantity, string> = { wheel_power_kw: "kW", wheel_torque_nm: "N m" };

/** Distinct strokes for overlaid runs; the n-th run takes the n-th. */
const RUN_STROKES = ["stroke-accent", "stroke-accent-2", "stroke-warn", "stroke-ok", "stroke-fg"];

const STATUS_TONE: Record<DynoPrerequisite["status"], string> = {
  recorded: "text-ok",
  stated: "text-fg-dim",
  estimated: "text-warn",
  pending: "text-warn",
  missing: "text-bad",
};

/** Every empty answer names its cause; these are the page's words for each. */
export function dynoEmptySentence(kind: DynoUnavailableKind, reason: string | null): string {
  const detail = reason ? ` ${reason}` : "";
  switch (kind) {
    case "not_configured":
      return `This vehicle is not set up for the dyno:${detail}`;
    case "over_budget":
      return "Every chosen session is over the analysis budget — analyse on the laptop.";
    case "no_pull_found":
      return "No pull found: the chosen sessions hold no sustained rpm rise.";
    case "every_pull_refused":
      return "Every rpm rise found was refused — each window below says why.";
    case "coefficients_pending":
      return `No power computed — coefficients pending.${detail}`;
    case "prerequisites_missing":
      return `No power computed — the vehicle profile is missing what it rests on.${detail}`;
    case "every_run_refused":
      return "Pulls were found but no run produced a curve — each run below says why.";
    default:
      return reason ?? "No result.";
  }
}

function finite(values: Array<number | null>): number[] {
  return values.filter((v): v is number => v !== null && Number.isFinite(v));
}

function points(
  rpm: Array<number | null>,
  values: Array<number | null>,
  x: (v: number) => number,
  y: (v: number) => number,
): string {
  const out: string[] = [];
  rpm.forEach((r, i) => {
    const v = values[i];
    if (r === null || v === null || v === undefined) return;
    out.push(`${x(r).toFixed(1)},${y(v).toFixed(1)}`);
  });
  return out.join(" ");
}

/** ±sd of the recorded same-day floor around a curve, band by band. */
function floorPolygon(
  curve: DynoCurve,
  quantity: DynoQuantity,
  floor: DynoFloor,
  x: (v: number) => number,
  y: (v: number) => number,
): string | null {
  if (!floor.available) return null;
  const upper: string[] = [];
  const lower: string[] = [];
  curve.rpm.forEach((r, i) => {
    const v = curve[quantity][i];
    if (r === null || v === null) return;
    const band = floor.bands.find((b) => r >= b.rpm_min && r <= b.rpm_max);
    if (!band) return;
    upper.push(`${x(r).toFixed(1)},${y(v + band.sd).toFixed(1)}`);
    lower.unshift(`${x(r).toFixed(1)},${y(v - band.sd).toFixed(1)}`);
  });
  return upper.length > 1 ? [...upper, ...lower].join(" ") : null;
}

export function DynoChart({ runs, quantity }: { runs: DynoRunOk[]; quantity: DynoQuantity }) {
  const W = 640;
  const H = 260;
  const pad = 36;
  const allRpm = runs.flatMap((run) => finite(run.curve.rpm));
  const allValues = runs.flatMap((run) => [
    ...finite(run.curve[quantity]),
    ...Object.values(run.per_heading).flatMap((c) => finite(c[quantity])),
  ]);
  if (!allRpm.length || !allValues.length) return null;
  const [rLo, rHi] = [Math.min(...allRpm), Math.max(...allRpm)];
  const [vLo, vHi] = [Math.min(0, ...allValues), Math.max(...allValues) * 1.1];
  const x = (r: number) => pad + ((r - rLo) / (rHi - rLo || 1)) * (W - 2 * pad);
  const y = (v: number) => H - pad - ((v - vLo) / (vHi - vLo || 1)) * (H - 2 * pad);
  return (
    <svg
      aria-label={`Dyno ${quantity === "wheel_power_kw" ? "power" : "torque"} against rpm`}
      width={W}
      height={H}
      className="bg-elev-2"
    >
      <line x1={pad} x2={W - pad} y1={y(0)} y2={y(0)} className="stroke-fg-dim" />
      <text x={pad} y={H - 8} fontSize={10} className="fill-fg-dim">
        {rLo.toFixed(0)} rpm
      </text>
      <text x={W - pad} y={H - 8} fontSize={10} textAnchor="end" className="fill-fg-dim">
        {rHi.toFixed(0)} rpm
      </text>
      <text x={4} y={pad - 8} fontSize={10} className="fill-fg-dim">
        {vHi.toFixed(0)} {UNIT[quantity]}
      </text>
      {runs.map((run, index) => {
        const stroke = RUN_STROKES[index % RUN_STROKES.length];
        const band = floorPolygon(run.curve, quantity, run.uncertainty.noise_floor[quantity], x, y);
        return (
          <g key={`${run.session_id}-${run.gear}`} data-run={`${run.session_id} gear ${run.gear}`}>
            {band && <polygon data-floor="band" points={band} className="fill-fg-dim/20" />}
            {Object.entries(run.per_heading).map(([heading, curve]) => (
              <polyline
                key={heading}
                data-heading={heading}
                points={points(curve.rpm, curve[quantity], x, y)}
                fill="none"
                strokeDasharray="3 3"
                strokeWidth={1}
                className={stroke}
              />
            ))}
            <polyline
              data-curve={run.direction}
              points={points(run.curve.rpm, run.curve[quantity], x, y)}
              fill="none"
              strokeWidth={2}
              className={stroke}
            />
          </g>
        );
      })}
    </svg>
  );
}

function peak(run: DynoRunOk, quantity: DynoQuantity): string {
  let best = -Infinity;
  let at: number | null = null;
  run.curve[quantity].forEach((v, i) => {
    const r = run.curve.rpm[i];
    if (v !== null && r !== null && v > best) {
      best = v;
      at = r;
    }
  });
  return at === null ? "—" : `${best.toFixed(1)} ${UNIT[quantity]} @ ${(at as number).toFixed(0)}`;
}

/** The tire pressures a run states, in psi as the rest of the app shows them (M01.8). */
export function tiresSentence(conditions: DynoRunOk["conditions"]): string {
  const psi = (kpa: number | null | undefined) =>
    kpa === null || kpa === undefined ? "not stated" : `${kpaToPsi(kpa).toFixed(1)} psi`;
  return (
    `tires front ${psi(conditions.tire_pressure_front_kpa)}, ` +
    `rear ${psi(conditions.tire_pressure_rear_kpa)}`
  );
}

/** What the operator typed in a tire psi box when it cannot be sent (M01.8). */
function TireReadingNote({ reading }: { reading: ReturnType<typeof readTirePsi> }) {
  if (reading.state !== "refused") return null;
  return (
    <span role="alert" className="max-w-[16rem] text-[0.68rem] leading-snug text-bad">
      {reading.message}
    </span>
  );
}

/** The road load's air scaling, mismatches and speed scale, one line (M01.8, M01.10). */
export function roadLoadAndScaleSentence(flags: DynoRunOk["flags"]): string {
  const air = flags.road_load_air;
  const scale = flags.speed_scale;
  const parts = [
    `road load C x${air.density_ratio.toFixed(3)} (coasted at ${air.fit_baro_kpa.toFixed(1)} kPa, ` +
      `run at ${air.run_baro_kpa.toFixed(1)} kPa)`,
    scale.speed_scale_residual === null || scale.power_band === null
      ? "speed scale unchecked"
      : `speed scale ${(100 * scale.speed_scale_residual).toFixed(1)} % ` +
        `(power up to ±${(100 * scale.power_band).toFixed(1)} %)` +
        (scale.n_unchecked > 0 ? `, ${scale.n_unchecked} pull(s) unchecked` : ""),
  ];
  return parts.join(" · ");
}

function floorSentence(floor: DynoFloor): string {
  if (floor.available) {
    return `same-day floor from ${floor.n_pull_pairs} pull pairs (${floor.bands.length} bands, shaded)`;
  }
  return `no floor: ${floor.reason}`;
}

function Prerequisites({ rows }: { rows: DynoPrerequisite[] }) {
  return (
    <ul aria-label="Prerequisites" className="m-0 mb-2 space-y-0.5 p-0 text-xs">
      {rows.map((row) => (
        <li key={row.fact} className="list-none" data-fact={row.fact}>
          <span className={cn("font-medium", STATUS_TONE[row.status])}>
            {row.fact.replace(/_/g, " ")}: {row.status}
          </span>
          {row.blocks_power && <span className="text-bad"> (blocks power)</span>}{" "}
          <span className="text-fg-dim">{row.detail}</span>
          {row.owed_by && <span className="text-fg-faint"> — owed by {row.owed_by}</span>}
        </li>
      ))}
    </ul>
  );
}

function exportCsv(result: DynoResponse) {
  const rows: Array<Array<string | number | null>> = [];
  for (const run of result.runs) {
    if (!run.available) continue;
    const curves: Array<[string, DynoCurve]> = [
      [run.direction, run.curve],
      ...Object.entries(run.per_heading),
    ];
    for (const [label, curve] of curves) {
      curve.rpm.forEach((rpm, i) => {
        rows.push([
          run.session_id,
          run.gear,
          label,
          rpm,
          curve.wheel_power_kw[i],
          curve.wheel_torque_nm[i],
          curve.engine_power_kw_estimate[i],
          curve.engine_torque_nm_estimate[i],
          run.conditions.tune_revision,
          run.conditions.road,
          run.flags.inertia,
        ]);
      });
    }
  }
  downloadText(
    toCsv(
      [
        "session",
        "gear",
        "curve",
        "rpm",
        "wheel_power_kw",
        "wheel_torque_nm",
        "engine_power_kw_estimate",
        "engine_torque_nm_estimate",
        "tune_revision",
        "road",
        "inertia",
      ],
      rows,
    ),
    "dyno.csv",
    "text/csv",
  );
}

function finiteMass(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** The vehicle mass the dyno adds payload to: ``config.vehicle.parameters.mass_kg``,
 *  which the daemon copied from the active profile when it started
 *  (``in_use``). ``saved`` is the profile's value now; the two differ after a
 *  Profiles edit until the daemon restarts. ``mass`` is ``null`` while
 *  loading, without a token, or when neither carries one. */
function useSavedVehicleMass(token: string): { mass: number | null; saved: number | null } {
  const fetcher = useCallback((signal: AbortSignal) => getProfiles(token, signal), [token]);
  const { data } = useControlResource(fetcher, { enabled: Boolean(token), intervalMs: 60000 });
  const active = data?.profiles.find((p) => p.slug === data.active_slug);
  const saved = finiteMass(active?.parameters?.mass_kg);
  const inUse = finiteMass(data?.in_use?.parameters?.mass_kg);
  return { mass: inUse ?? saved, saved };
}

/** H07.9: wheel power and torque against rpm per pull pair, runs overlaid,
 *  with the prerequisites first — a pending coefficient is shown pending and
 *  no curve is drawn from it. */
export function DynoView({ token, choice }: { token: string; choice: SessionChoice }) {
  const { result, refusal, busy, run } = useAnalysisRun<DynoResponse>();
  const [road, setRoad] = useState("");
  const [payload, setPayload] = useState("");
  const { mass: vehicleMass, saved: savedMass } = useSavedVehicleMass(token);
  const payloadKg = payload.trim() ? Number(payload) : null;
  const [induction, setInduction] = useState<"" | "na" | "boosted">("");
  const [ambient, setAmbient] = useState("");
  // The operator sets tire pressure in psi; the request carries kPa (M01.8).
  const [tireFront, setTireFront] = useState("");
  const [tireRear, setTireRear] = useState("");
  const front = readTirePsi(tireFront);
  const rear = readTirePsi(tireRear);
  const [single, setSingle] = useState(false);
  const [quantity, setQuantity] = useState<DynoQuantity>("wheel_power_kw");
  const ready = Boolean(token && (choice.sessions.length || choice.revision));
  const computed = useMemo(
    () => (result ? result.runs.filter((r): r is DynoRunOk => r.available) : []),
    [result],
  );
  const refusedRuns = result ? result.runs.filter((r) => !r.available) : [];
  return (
    <Panel
      title="Driving dyno"
      actions={
        <SegmentedControl
          label="Dyno quantity"
          options={QUANTITIES}
          value={quantity}
          onChange={setQuantity}
        />
      }
    >
      <div className="mb-3 flex flex-wrap items-end gap-2 text-xs">
        <label className="flex flex-col gap-1">
          Road
          <input
            className={inputClass}
            aria-label="Road"
            placeholder="which road, both ways"
            value={road}
            onChange={(e) => setRoad(e.target.value)}
          />
        </label>
        <label className="flex flex-col gap-1">
          Payload (kg), added to the vehicle
          <input
            className={inputClass}
            aria-label="Payload (kg)"
            inputMode="decimal"
            placeholder="driver, passengers, cargo"
            value={payload}
            onChange={(e) => setPayload(e.target.value)}
          />
          <span className="max-w-[16rem] text-[0.68rem] leading-snug text-fg-faint">
            {vehicleMass === null
              ? "Saved vehicle weight: not loaded. Payload is added to the weight in Profiles."
              : `Saved vehicle weight ${vehicleMass.toLocaleString()} kg (Profiles). Enter only what was aboard on top of it.`}
            {vehicleMass !== null && payloadKg !== null && Number.isFinite(payloadKg)
              ? ` Test mass: ${(vehicleMass + payloadKg).toLocaleString()} kg.`
              : ""}
            {vehicleMass !== null && savedMass !== null && savedMass !== vehicleMass
              ? ` Profiles now holds ${savedMass.toLocaleString()} kg; the dyno uses it after a daemon restart.`
              : ""}
          </span>
        </label>
        <label className="flex flex-col gap-1">
          Induction
          <select
            className={selectClass}
            aria-label="Induction"
            value={induction}
            onChange={(e) => setInduction(e.target.value as "" | "na" | "boosted")}
          >
            <option value="">state it</option>
            <option value="na">NA</option>
            <option value="boosted">boosted</option>
          </select>
        </label>
        <label className="flex flex-col gap-1">
          Ambient °C (if not recorded)
          <input
            className={inputClass}
            aria-label="Ambient °C"
            inputMode="decimal"
            placeholder="from the session"
            value={ambient}
            onChange={(e) => setAmbient(e.target.value)}
          />
        </label>
        <label className="flex flex-col gap-1">
          Front tires (psi)
          <input
            className={inputClass}
            aria-label="Front tires (psi)"
            inputMode="decimal"
            placeholder="as set, not stated"
            value={tireFront}
            onChange={(e) => setTireFront(e.target.value)}
          />
          <TireReadingNote reading={front} />
        </label>
        <label className="flex flex-col gap-1">
          Rear tires (psi)
          <input
            className={inputClass}
            aria-label="Rear tires (psi)"
            inputMode="decimal"
            placeholder="as set, not stated"
            value={tireRear}
            onChange={(e) => setTireRear(e.target.value)}
          />
          <TireReadingNote reading={rear} />
        </label>
        <label className="flex items-center gap-1.5">
          <input type="checkbox" checked={single} onChange={(e) => setSingle(e.target.checked)} />
          Allow one direction (labelled single)
        </label>
        <button
          type="button"
          className={buttonClass}
          disabled={!ready || busy || front.state === "refused" || rear.state === "refused"}
          onClick={() =>
            run((signal) =>
              getDyno(
                token,
                choice,
                {
                  road: road.trim() || null,
                  payload_kg: payload.trim() ? Number(payload) : null,
                  induction: induction || null,
                  ambient_temp_c: ambient.trim() ? Number(ambient) : null,
                  allow_single_direction: single,
                  tire_pressure_front_kpa: front.state === "ok" ? front.kpa : null,
                  tire_pressure_rear_kpa: rear.state === "ok" ? rear.kpa : null,
                },
                signal,
              ),
            )
          }
        >
          {busy ? "Running…" : "Run"}
        </button>
      </div>
      <RefusalNotice refusal={refusal} />
      {result && (
        <>
          <Prerequisites rows={result.prerequisites} />
          {result.available ? (
            <>
              <DynoChart runs={computed} quantity={quantity} />
              <p className="m-0 mt-1 text-xs text-fg-faint">
                Solid: the run (direction pair, or labelled single). Dashed: each heading. Shaded:
                the same-day noise floor where one is recorded for the regime. Engine figures are
                wheel ÷ driveline efficiency — estimates.
              </p>
            </>
          ) : (
            <p
              role="status"
              data-empty={result.unavailable_kind ?? "none"}
              className={cn(
                "m-0 text-sm",
                result.unavailable_kind === "coefficients_pending" ? "text-warn" : "text-bad",
              )}
            >
              {dynoEmptySentence(result.unavailable_kind, result.reason)}
            </p>
          )}
          {computed.length > 0 && (
            <TableScroll className="mt-2">
              <table aria-label="Dyno runs" className="w-full text-left text-xs">
                <thead>
                  <tr>
                    <th>Session</th>
                    <th>Gear</th>
                    <th>Run</th>
                    <th>Peak</th>
                    <th>Conditions</th>
                    <th>Flags</th>
                    <th>Uncertainty</th>
                  </tr>
                </thead>
                <tbody>
                  {computed.map((r) => (
                    <tr key={`${r.session_id}-${r.gear}`}>
                      <td>{r.session_id}</td>
                      <td>{r.gear}</td>
                      <td>
                        {r.direction} · {r.n_pulls} pulls
                      </td>
                      <td>{peak(r, quantity)}</td>
                      <td>
                        {r.conditions.tune_revision} · {r.conditions.induction} ·{" "}
                        {r.conditions.road} · {r.conditions.baro_kpa.toFixed(1)} kPa · IAT{" "}
                        {r.conditions.intake_temp_c.toFixed(0)} °C · ambient{" "}
                        {r.conditions.ambient_temp_c.toFixed(0)} °C · payload{" "}
                        {r.conditions.payload_kg} kg · {tiresSentence(r.conditions)} ·{" "}
                        {r.conditions.date}
                      </td>
                      <td>
                        inertia {r.flags.inertia} · squat{" "}
                        {r.flags.squat_corrected ? "corrected" : "not corrected"} · pitch offset{" "}
                        {r.flags.imu_pitch_offset_deg.toFixed(2)}° (
                        {r.flags.imu_pitch_offset_source}) · {roadLoadAndScaleSentence(r.flags)}
                        {r.flags.road_load_mismatches.map((m) => (
                          // "differs" is a finding; "unstated" is a gap the operator left
                          // (the tire fields are optional), so it must not read as a warning.
                          <span
                            key={m.field}
                            data-mismatch={m.status}
                            className={cn(
                              "block",
                              m.status === "differs" ? "text-warn" : "text-fg-faint",
                            )}
                          >
                            {m.detail}
                          </span>
                        ))}
                      </td>
                      <td>{floorSentence(r.uncertainty.noise_floor[quantity])}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableScroll>
          )}
          {refusedRuns.length > 0 && (
            <ul aria-label="Refused runs" className="m-0 mt-2 space-y-0.5 p-0 text-xs">
              {refusedRuns.map((r) => (
                <li key={`${r.session_id}-${r.gear}`} className="list-none text-fg-dim">
                  <code>{r.session_id}</code> gear {r.gear} —{" "}
                  <span className="text-warn">{r.kind.replace(/_/g, " ")}</span>: {r.reason}
                </li>
              ))}
            </ul>
          )}
          {result.sessions.map((session) => (
            <details key={session.session_id} className="mt-2 text-xs text-fg-dim">
              <summary>
                {session.session_id}: {session.n_windows} rpm rise(s) found, {session.n_pulls}{" "}
                usable pull(s) · {session.revision ?? "unknown revision"}
              </summary>
              <ul className="mt-1 space-y-0.5">
                {session.windows.map((w) => (
                  <li key={w.start}>
                    {w.start.slice(11, 19)} {w.rpm_start?.toFixed(0)}→{w.rpm_end?.toFixed(0)} rpm
                    {w.course_deg !== null && ` · course ${w.course_deg.toFixed(0)}°`}
                    {w.refusal
                      ? ` — ${w.refusal.kind.replace(/_/g, " ")}: ${w.refusal.reason}`
                      : ` — gear ${w.gear}, ${w.heading}`}
                  </li>
                ))}
              </ul>
            </details>
          ))}
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              className={buttonClass}
              disabled={computed.length === 0}
              onClick={() => exportCsv(result)}
            >
              Export CSV
            </button>
            <button
              type="button"
              className={buttonClass}
              onClick={() =>
                downloadText(JSON.stringify(result, null, 2), "dyno.json", "application/json")
              }
            >
              Export JSON
            </button>
          </div>
          <UnavailableList items={result.unavailable} />
          <ConditionsBlock conditions={result.conditions} />
        </>
      )}
    </Panel>
  );
}
