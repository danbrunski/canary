import { useCallback, useEffect, useState } from "react";

import { estimateTuning, getTuning, revertTuning, updateTuning } from "../api/http";
import { Notice } from "../components/Notice";
import { Panel } from "../components/Panel";
import { useAsyncAction } from "../hooks/useAsyncAction";
import { useControlResource } from "../hooks/useControlResource";
import { buttonClass, cn, inputClass, placeholderClass } from "../styles";
import type { TuningConfig, TuningEstimate } from "../types";

interface TuningPanelProps {
  token: string;
}

/** Threshold tuning & suppression (Task 6.25). The config is edited as JSON
 *  (the rule shapes are small and the backend is the strict validator) with
 *  a mandatory **estimate** step — before/after suppressed counts over
 *  recent anomalies — so the operator sees the blast radius before
 *  committing (Risk R11). Apply auto-versions + audits server-side; revert
 *  is itself a new audited version. */
export function TuningPanel({ token }: TuningPanelProps) {
  const fetcher = useCallback(
    (signal: AbortSignal) => getTuning(token, signal),
    [token],
  );
  const { data, error, loading, refresh } = useControlResource(fetcher, {
    enabled: Boolean(token),
    intervalMs: 30000,
  });

  const [draft, setDraft] = useState("");
  const [estimate, setEstimate] = useState<TuningEstimate | null>(null);
  const { notice, busy, run } = useAsyncAction();
  const [revertVersion, setRevertVersion] = useState("");

  // Seed the editor from the live config the first time it loads / changes
  // version (don't clobber an in-progress edit on every poll).
  const liveVersion = data?.tuning.version;
  useEffect(() => {
    if (data) setDraft(JSON.stringify(data.tuning, null, 2));
  }, [liveVersion, data]);

  if (!token) {
    return (
      <Panel title="Tuning">
        <p className={placeholderClass}>
          Enter the control token (Control panel) to view / edit tuning.
        </p>
      </Panel>
    );
  }
  if (loading) {
    return (
      <Panel title="Tuning">
        <p className={placeholderClass}>loading tuning config…</p>
      </Panel>
    );
  }
  if (error || !data) {
    return (
      <Panel title="Tuning">
        <p className={cn(placeholderClass, "text-bad")}>{error ?? "no data"}</p>
      </Panel>
    );
  }

  const parseDraft = (): TuningConfig => {
    const parsed = JSON.parse(draft) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("Tuning config must be a JSON object.");
    }
    return parsed as TuningConfig;
  };

  const onEstimate = () =>
    run("estimate", async () => {
      setEstimate(await estimateTuning(token, parseDraft()));
    });

  const onApply = () =>
    run("apply", async () => {
      await updateTuning(token, parseDraft(), "dashboard edit");
      setEstimate(null);
      refresh();
    });

  const onRevert = () =>
    run("revert", async () => {
      const v = Number(revertVersion);
      if (!Number.isInteger(v) || v < 1) throw new Error("Enter a version ≥ 1.");
      await revertTuning(token, v);
      setRevertVersion("");
      refresh();
    });

  const cfg = data.tuning;

  return (
    <Panel
      title="Tuning"
      count={`v${cfg.version} · ${cfg.threshold_overrides.length} ovr · ${cfg.suppression_rules.length} rules · ${cfg.debounce_policies.length} pol`}
    >
      <Notice notice={notice} className="mb-2" />

      <textarea
        className={cn(inputClass, "h-72 w-full resize-y font-num text-[0.78rem]")}
        value={draft}
        spellCheck={false}
        onChange={(e) => setDraft(e.target.value)}
        aria-label="Tuning config JSON"
      />

      <div className="mt-2 flex flex-wrap gap-2">
        <button
          type="button"
          className={buttonClass}
          onClick={onEstimate}
          disabled={Boolean(busy)}
        >
          Estimate
        </button>
        <button
          type="button"
          className={cn(buttonClass, "border-accent text-accent")}
          onClick={onApply}
          disabled={Boolean(busy)}
          title="Auto-versioned + audited server-side"
        >
          Apply
        </button>
        <span className="ml-auto flex items-center gap-1.5">
          <input
            className={cn(inputClass, "w-24")}
            value={revertVersion}
            onChange={(e) => setRevertVersion(e.target.value)}
            inputMode="numeric"
            placeholder="version"
            aria-label="Revert to version"
          />
          <button
            type="button"
            className={buttonClass}
            onClick={onRevert}
            disabled={Boolean(busy) || !revertVersion}
          >
            Revert
          </button>
        </span>
      </div>

      {estimate && (
        <div className="mt-3 rounded-[8px] border border-accent-2/45 bg-accent-2/10 p-3 text-[0.84rem]">
          <b className="text-accent-2">Before / after estimate</b>
          <div className="mt-1 font-num tabular-nums text-fg-dim">
            evaluated {estimate.before.evaluated} · would-suppress{" "}
            {estimate.before.would_suppress} →{" "}
            <b className="text-fg">{estimate.after.would_suppress}</b>{" "}
            <span
              className={
                estimate.newly_suppressed > 0 ? "text-warn" : "text-ok"
              }
            >
              ({estimate.newly_suppressed >= 0 ? "+" : ""}
              {estimate.newly_suppressed} newly muted)
            </span>
          </div>
          <p className="mt-1 text-[0.76rem] text-fg-faint">
            Review this before Apply — tuning must not silently blind the
            detector (Risk R11).
          </p>
        </div>
      )}
    </Panel>
  );
}
