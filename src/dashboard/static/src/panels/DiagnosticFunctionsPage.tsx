import { useCallback, useState } from "react";

import { clearModuleCodes, getDiagnosticFunctions } from "../api/http";
import { Panel } from "../components/Panel";
import { useControlResource } from "../hooks/useControlResource";
import { buttonClass, cn, dangerButtonClass, placeholderClass } from "../styles";
import type { ClearCodesResult, DiagnosticFunction } from "../types/diagnostics";

interface DiagnosticFunctionsPageProps {
  token: string;
}

/** Diagnostic functions a scan tool runs on a module, done by CANary itself.
 *  Today: clearing a K-line module's stored codes, offered only for a module
 *  whose vehicle profile records the clear a scan tool was seen to send (the
 *  Xterra's airbag module, 2026-10-04). Clearing codes is routine diagnostic
 *  work (AGENTS.md), so there is no vehicle-state gate, only a confirm. */
export function DiagnosticFunctionsPage({ token }: DiagnosticFunctionsPageProps) {
  const fetcher = useCallback(
    (signal: AbortSignal) => getDiagnosticFunctions(token, signal),
    [token],
  );
  const { data, error, loading, refresh } = useControlResource(fetcher, {
    enabled: Boolean(token),
    intervalMs: 5000,
  });

  if (!token) {
    return (
      <Panel title="Diagnostic functions">
        <p className={placeholderClass}>
          Enter the control token (Control panel) to run diagnostic functions.
        </p>
      </Panel>
    );
  }
  if (loading && !data) {
    return (
      <Panel title="Diagnostic functions">
        <p className={placeholderClass}>reading the vehicle profile…</p>
      </Panel>
    );
  }
  if (error || !data) {
    return (
      <Panel title="Diagnostic functions">
        <p className={cn(placeholderClass, "text-bad")}>{error ?? "no data"}</p>
      </Panel>
    );
  }

  const { functions, kline_available, blocked_reason } = data.diagnostic_functions;
  const unavailable = !kline_available
    ? "The K-line collector is not running (hardware.kline.enabled)."
    : blocked_reason
      ? `K-line unavailable: ${blocked_reason}.`
      : null;

  return (
    <Panel title="Diagnostic functions">
      {unavailable && (
        <p role="status" className="mb-3 text-[0.82rem] text-warn">
          {unavailable}
        </p>
      )}
      {functions.length === 0 ? (
        <p className={placeholderClass}>
          This vehicle&apos;s profile records no diagnostic functions yet.
        </p>
      ) : (
        <div className="flex flex-col gap-3">
          {functions.map((fn) => (
            <ClearCodesCard
              key={`${fn.kind}-${fn.address}`}
              fn={fn}
              token={token}
              disabled={unavailable !== null}
              onDone={refresh}
            />
          ))}
        </div>
      )}
    </Panel>
  );
}

const MODULE_NAMES: Record<string, string> = {
  SRS: "Airbag (SRS)",
  ABS: "ABS",
  ECM: "Engine (ECM)",
};

function ClearCodesCard({
  fn,
  token,
  disabled,
  onDone,
}: {
  fn: DiagnosticFunction;
  token: string;
  disabled: boolean;
  onDone: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ClearCodesResult | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const name = MODULE_NAMES[fn.module] ?? fn.module;

  const clear = async () => {
    setConfirming(false);
    setBusy(true);
    setFailure(null);
    setResult(null);
    try {
      const response = await clearModuleCodes(token, fn.module);
      setResult(response.clear_codes);
    } catch (err) {
      setFailure(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
      onDone();
    }
  };

  return (
    <div className="rounded-[8px] border border-border-soft p-3">
      <div className="mb-1 flex items-baseline justify-between gap-2">
        <h3 className="text-[0.92rem] font-semibold text-fg">Clear {name} codes</h3>
        <span className="font-num text-[0.74rem] text-fg-faint">
          K-line {fn.address} · sends {fn.request_hex}
        </span>
      </div>
      <p className="mb-2 text-[0.8rem] text-fg-dim">
        Erases the module&apos;s stored codes. A fault that is still present sets its code again and
        keeps the warning lamp on.
      </p>
      <div className="flex flex-wrap items-center gap-2">
        {confirming ? (
          <>
            <button
              type="button"
              className={dangerButtonClass}
              disabled={busy || disabled}
              onClick={() => void clear()}
            >
              Confirm: clear {name} codes
            </button>
            <button type="button" className={buttonClass} onClick={() => setConfirming(false)}>
              Cancel
            </button>
          </>
        ) : (
          <button
            type="button"
            className={buttonClass}
            disabled={busy || disabled}
            onClick={() => setConfirming(true)}
          >
            {busy ? "Clearing…" : "Clear codes"}
          </button>
        )}
      </div>
      {result && (
        <p role="status" className={cn("mt-2 text-[0.82rem]", result.ok ? "text-ok" : "text-bad")}>
          {result.ok
            ? `Cleared: ${result.module} answered ${result.reply_hex ?? "positively"} in ${result.duration_s.toFixed(1)} s.`
            : `Not cleared: ${result.detail}`}
        </p>
      )}
      {failure && <p className="mt-2 text-[0.82rem] text-bad">{failure}</p>}
      <details className="mt-2 text-[0.74rem] text-fg-faint">
        <summary>Where this comes from</summary>
        {fn.evidence}
      </details>
    </div>
  );
}
