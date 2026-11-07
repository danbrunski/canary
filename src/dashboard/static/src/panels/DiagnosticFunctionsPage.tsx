import { useCallback, useState } from "react";

import { clearModuleCodes, getDiagnosticFunctions, readModuleCodes } from "../api/http";
import { Panel } from "../components/Panel";
import { useControlResource } from "../hooks/useControlResource";
import { buttonClass, cn, dangerButtonClass, placeholderClass } from "../styles";
import type {
  ClearCodesResult,
  DiagnosticExchange,
  DiagnosticFunction,
  ReadCodesResult,
} from "../types/diagnostics";
import { TokenRequired } from "../components/TokenRequired";

interface DiagnosticFunctionsPageProps {
  token: string;
}

/** Diagnostic functions a scan tool runs on a module, done by CANary itself.
 *  Today: reading and clearing a K-line module's stored codes, each offered
 *  only for a module whose vehicle profile records the requests a scan tool
 *  was seen to send (the Xterra's airbag module: clear 2026-10-04, read
 *  2026-10-08). Both are routine diagnostic work (AGENTS.md), so there is no
 *  vehicle-state gate; a clear asks for a confirm. */
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
        <TokenRequired purpose="run diagnostic functions" />
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
          {byModule(functions).map((group) => (
            <ModuleCard
              key={group.address}
              functions={group.functions}
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

interface ModuleFunctions {
  address: string;
  functions: DiagnosticFunction[];
}

/** The functions grouped by module, in the order the daemon listed them. */
function byModule(functions: DiagnosticFunction[]): ModuleFunctions[] {
  const groups = new Map<string, DiagnosticFunction[]>();
  for (const fn of functions) {
    const group = groups.get(fn.address);
    if (group) group.push(fn);
    else groups.set(fn.address, [fn]);
  }
  return [...groups].map(([address, fns]) => ({ address, functions: fns }));
}

interface ActionProps {
  fn: DiagnosticFunction;
  token: string;
  disabled: boolean;
  onDone: () => void;
}

function ModuleCard({
  functions,
  token,
  disabled,
  onDone,
}: {
  functions: DiagnosticFunction[];
  token: string;
  disabled: boolean;
  onDone: () => void;
}) {
  const first = functions[0];
  const name = MODULE_NAMES[first.module] ?? first.module;
  return (
    <div className="rounded-[8px] border border-border-soft p-3">
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <h3 className="text-[0.92rem] font-semibold text-fg">{name}</h3>
        <span className="font-num text-[0.74rem] text-fg-faint">K-line {first.address}</span>
      </div>
      <div className="flex flex-col gap-3">
        {functions.map((fn) =>
          fn.kind === "read_codes" ? (
            <ReadCodesAction
              key={fn.kind}
              fn={fn}
              token={token}
              disabled={disabled}
              onDone={onDone}
            />
          ) : (
            <ClearCodesAction
              key={fn.kind}
              fn={fn}
              token={token}
              disabled={disabled}
              onDone={onDone}
            />
          ),
        )}
      </div>
    </div>
  );
}

function RawExchange({ raw }: { raw: DiagnosticExchange[] | undefined }) {
  if (!raw || raw.length === 0) return null;
  return (
    <details className="mt-1 text-[0.74rem] text-fg-faint">
      <summary>Raw exchange</summary>
      <pre className="font-num whitespace-pre-wrap">
        {raw.map((x) => `${x.request_hex} → ${x.reply_hex ?? "(no reply)"}`).join("\n")}
      </pre>
    </details>
  );
}

function ReadCodesAction({ fn, token, disabled, onDone }: ActionProps) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ReadCodesResult | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const name = MODULE_NAMES[fn.module] ?? fn.module;

  const read = async () => {
    setBusy(true);
    setFailure(null);
    setResult(null);
    try {
      const response = await readModuleCodes(token, fn.module);
      setResult(response.read_codes);
    } catch (err) {
      setFailure(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
      onDone();
    }
  };

  const requests = fn.requests ?? [];
  const statuses = requests.map((r) => r.status).join(" and ");
  const inferred = requests.some((r) => r.status_confidence === "inferred");

  return (
    <div>
      <div className="mb-1 flex items-baseline justify-between gap-2">
        <h4 className="text-[0.86rem] font-semibold text-fg">Read {name} codes</h4>
        <span className="font-num text-[0.74rem] text-fg-faint">sends {fn.request_hex}</span>
      </div>
      <p className="mb-2 text-[0.8rem] text-fg-dim">
        Reads the module&apos;s stored codes{statuses ? ` (${statuses})` : ""}.
        {inferred && " Whether a code is current or past is inferred, not confirmed."}
      </p>
      <button
        type="button"
        className={buttonClass}
        disabled={busy || disabled}
        onClick={() => void read()}
      >
        {busy ? "Reading…" : "Read codes"}
      </button>
      {result && <ReadCodesOutcome result={result} />}
      {failure && <p className="mt-2 text-[0.82rem] text-bad">{failure}</p>}
      <details className="mt-2 text-[0.74rem] text-fg-faint">
        <summary>Where this comes from</summary>
        {fn.evidence}
      </details>
    </div>
  );
}

function ReadCodesOutcome({ result }: { result: ReadCodesResult }) {
  if (!result.ok || result.codes === null) {
    return (
      <div className="mt-2">
        <p role="status" className="text-[0.82rem] text-bad">
          Not read: {result.detail}
        </p>
        <RawExchange raw={result.raw} />
      </div>
    );
  }
  return (
    <div className="mt-2">
      {result.codes.length === 0 ? (
        <p role="status" className="text-[0.82rem] text-ok">
          No codes stored ({result.module} read in {result.duration_s.toFixed(1)} s).
        </p>
      ) : (
        <ul role="status" className="flex flex-col gap-0.5 text-[0.82rem]">
          {result.codes.map((c) => (
            <li key={`${c.code}-${c.status}`} className="text-warn">
              <span className="font-num font-semibold">{c.code}</span>{" "}
              <span className="uppercase">{c.status}</span>
              {c.status_confidence === "inferred" && (
                <span className="text-fg-faint"> (status inferred)</span>
              )}{" "}
              <span className="font-num text-fg-faint">{c.word}</span>
            </li>
          ))}
        </ul>
      )}
      <RawExchange raw={result.raw} />
    </div>
  );
}

function ClearCodesAction({ fn, token, disabled, onDone }: ActionProps) {
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
    <div>
      <div className="mb-1 flex items-baseline justify-between gap-2">
        <h4 className="text-[0.86rem] font-semibold text-fg">Clear {name} codes</h4>
        <span className="font-num text-[0.74rem] text-fg-faint">sends {fn.request_hex}</span>
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
