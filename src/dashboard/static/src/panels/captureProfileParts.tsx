// Pieces the capture runner (Logging) and the profile editor (System) share
// (R08.6): the labelled field, the profile picker, the panel status line and
// the plan summary.

import type { ReactNode } from "react";

import { inputClass } from "../styles";
import type { CaptureProfile, CaptureProfileState, ResolvedPollingPlan } from "../types";

/** A labelled control sized to its content, with the units and the
 *  blank-means-what note demoted out of the label so the label stays short. */
export function Field({
  label,
  hint,
  className = "",
  children,
}: {
  label: string;
  hint?: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <label className={`text-sm text-fg-dim ${className}`}>
      <span className="block">{label}</span>
      {children}
      {hint && <span className="mt-0.5 block text-[0.68rem] text-fg-dim/80">{hint}</span>}
    </label>
  );
}

export function ProfilePicker({
  profiles,
  value,
  onChange,
  hint,
  disabled = false,
}: {
  profiles: CaptureProfile[];
  value: string;
  onChange: (name: string) => void;
  hint: string;
  disabled?: boolean;
}) {
  return (
    <Field label="Profile" hint={hint} className="max-[600px]:w-full">
      <select
        className={`${inputClass} mt-1 w-56 max-[600px]:w-full`}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
      >
        {profiles.map((profile) => (
          <option key={profile.name}>{profile.name}</option>
        ))}
      </select>
    </Field>
  );
}

/** The panel header's "what is collecting now" line. */
export function CollectionStatus({ active }: { active: CaptureProfileState | null }) {
  return (
    <span className={active?.active ? "text-warn" : "text-fg-dim"}>
      {active?.active
        ? `active: ${active.name}`
        : active?.name === "default"
          ? "default collection"
          : "status unavailable"}
    </span>
  );
}

type Plan = CaptureProfile | CaptureProfileState;

function sessionPolicy(plan: Plan, mode01Count: number, diagnosticCount: number): string {
  if (diagnosticCount === 0) return "Mode 01 only";
  if (plan.did_continuous) return "Mode 22 session held open";
  return mode01Count > 0 ? "Mode 01 / Mode 22 alternating" : "Mode 22 windowed";
}

/** What a profile (or the running capture) polls, at what cadence and under
 *  which session policy, with the daemon's resolved demand when known. */
export function CapturePlanSummary({
  plan,
  resolved,
}: {
  plan: Plan;
  resolved: ResolvedPollingPlan | null | undefined;
}) {
  const mode01Count = plan.mode01_enabled ? Object.keys(plan.mode01).length : 0;
  const mode22Count = plan.mode22_enabled ? plan.mode22.length : 0;
  const memoryCount = plan.mode22_enabled ? (plan.memory?.length ?? 0) : 0;
  return (
    <div className="rounded border border-border p-3 text-sm" data-testid="capture-plan">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-semibold">Capture plan: {plan.name}</span>
        {plan.experimental && (
          <span className="rounded border border-warn/60 bg-warn/10 px-1.5 py-0.5 text-[0.68rem] font-semibold uppercase text-warn">
            Experimental
          </span>
        )}
      </div>
      {plan.description && <p className="mt-1 text-fg-dim">{plan.description}</p>}
      {plan.notes && <p className="mt-1 text-xs text-fg-dim">{plan.notes}</p>}
      <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-fg-dim">
        <span>{mode01Count} Mode 01 channels</span>
        <span>{mode22Count} Mode 22 DIDs</span>
        <span>{memoryCount} memory channels</span>
        <span>
          {plan.target_rate_hz == null ? "relative cadence" : `${plan.target_rate_hz} Hz/channel`}
        </span>
        <span>{sessionPolicy(plan, mode01Count, mode22Count + memoryCount)}</span>
        {plan.did_interval_seconds != null && (
          <span>DID window every {plan.did_interval_seconds}s</span>
        )}
        {plan.bus_budget_override != null && (
          <span>{plan.bus_budget_override} requests/s budget</span>
        )}
        {plan.expected_total_demand_hz != null && (
          <span>{plan.expected_total_demand_hz} expected channel reads/s</span>
        )}
        <span>{plan.corpus_eligible ? "training eligible" : "training corpus excluded"}</span>
        {plan.min_achieved_rate_hz != null && (
          <span>{plan.min_achieved_rate_hz} Hz floor enforced</span>
        )}
        {plan.requires_tune_revision && <span>tune revision required</span>}
        {plan.behavior_model_recipes.length > 0 && (
          <span>{plan.behavior_model_recipes.length} behavior-model recipes allowed</span>
        )}
      </div>
      {mode01Count > 0 && (
        <p className="mt-2 text-xs text-fg-dim">Mode 01: {Object.keys(plan.mode01).join(", ")}</p>
      )}
      {mode22Count > 0 && (
        <p className="mt-2 text-xs text-fg-dim">Mode 22: {plan.mode22.join(", ")}</p>
      )}
      {memoryCount > 0 && (
        <p className="mt-2 text-xs text-fg-dim">
          RAM reads (service 0x23): {(plan.memory ?? []).join(", ")}
        </p>
      )}
      {resolved && <ResolvedDemand resolved={resolved} />}
      {plan.required_tuple.length > 0 && (
        /* The joint requirement, shown because it is the thing per-channel
           rates cannot express: these must land in the same poll round or
           the capture yields nothing regardless of how healthy each looks. */
        <p className="mt-2 text-xs text-fg-dim">
          Required together: {plan.required_tuple.join(", ")}
        </p>
      )}
      {plan.capacity_evidence && (
        <p className="mt-2 text-xs text-fg-dim">{plan.capacity_evidence}</p>
      )}
    </div>
  );
}

function ResolvedDemand({ resolved }: { resolved: ResolvedPollingPlan }) {
  return (
    <div
      className={`mt-2 rounded border p-2 text-xs ${resolved.best_case_feasible ? "border-border" : "border-bad/60 text-bad"}`}
    >
      <span>{resolved.channel_reads_per_second} channel reads/s</span>
      {" · "}
      <span>{resolved.diagnostic_requests_per_second.best_case_batched} requests/s batched</span>
      {" · "}
      <span>
        {resolved.diagnostic_requests_per_second.single_request_fallback} requests/s fallback
      </span>
      {" · "}
      <span>
        {resolved.raw_frames_per_second.best_case_batched} raw frames/s
        {resolved.raw_frames_per_second.estimate_complete ? "" : " (lower bound; unknown widths)"}
      </span>
      {" · "}
      <span>{resolved.decoded_rows_per_second} decoded rows/s</span>
      {resolved.complete_tuples_per_second != null && (
        <>
          {" · "}
          <span>{resolved.complete_tuples_per_second} complete tuples/s requested</span>
        </>
      )}
      {!resolved.priority_feasible && (
        <p className="mt-1 font-semibold">Priority demand alone exceeds the bus budget.</p>
      )}
      {!resolved.best_case_feasible && (
        <p className="mt-1 font-semibold">Plan is infeasible even with configured batching.</p>
      )}
      {resolved.best_case_feasible && !resolved.fallback_feasible && (
        <p className="mt-1 text-warn">
          Single-request fallback exceeds the budget; quality will report rate shortfall if batching
          falls back.
        </p>
      )}
    </div>
  );
}
