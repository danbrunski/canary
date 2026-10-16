import { useState } from "react";

import { getDeliveryError } from "../../api/workbench";
import { Panel } from "../../components/Panel";
import { useAnalysisRun } from "../../hooks/useWorkbench";
import { buttonClass, inputClass, selectClass } from "../../styles";
import type { DeliveryErrorResponse, SessionChoice } from "../../types/workbench";
import { DeliveryChart } from "./DeliveryChart";
import { DelayStability, PairingNote } from "./DeliveryNotes";
import { FloorNote } from "./NoiseFloorNotes";
import { ConditionsBlock, EmptyResult, RefusalNotice, UnavailableList } from "./WorkbenchNotices";

/** The measured-AFR sources, named — never a silent default. The ECM's own λ
 *  and the A/F-voltage path disagree by ~3.8 %, so the page makes the
 *  operator pick a side and every result carries the side picked. */
export const MEASURED_SOURCES: ReadonlyArray<{ id: string; label: string }> = [
  { id: "obd:O2_WR_LAMBDA_S1", label: "ECM λ — Mode 01 wide-range O2 (obd:O2_WR_LAMBDA_S1)" },
  { id: "semantic:measured_afr_b1", label: "A/F sensor voltage, bank 1, calibrated transfer" },
  { id: "semantic:measured_afr_b2", label: "A/F sensor voltage, bank 2, calibrated transfer" },
];

/** Delivery error by rpm band for the chosen sessions, against a measured-AFR
 *  source the operator picks. Owns its own run (result, refusal, busy). */
export function DeliveryView({ token, choice }: { token: string; choice: SessionChoice }) {
  const [measured, setMeasured] = useState("");
  const [minLoad, setMinLoad] = useState("");
  const { result, refusal, busy, run } = useAnalysisRun<DeliveryErrorResponse>();
  const semantic = measured.startsWith("semantic:");
  const ready = Boolean(token && measured && (choice.sessions.length || choice.revision));
  return (
    <Panel title="Delivery error by rpm band">
      <div className="mb-3 flex flex-wrap items-end gap-2 text-xs">
        <label className="flex min-w-72 flex-col gap-1">
          Measured-AFR source
          <select
            className={selectClass}
            aria-label="Measured-AFR source"
            value={measured}
            onChange={(e) => setMeasured(e.target.value)}
          >
            <option value="">Choose a source — the two paths disagree by ~3.8 %</option>
            {MEASURED_SOURCES.map((source) => (
              <option key={source.id} value={source.id}>
                {source.label}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          Min load (Mode 01 only)
          <input
            className={inputClass}
            inputMode="decimal"
            placeholder="none"
            disabled={semantic}
            value={semantic ? "" : minLoad}
            onChange={(e) => setMinLoad(e.target.value)}
          />
        </label>
        <button
          type="button"
          className={buttonClass}
          disabled={!ready || busy}
          onClick={() =>
            run((signal) =>
              getDeliveryError(
                token,
                choice,
                measured,
                !semantic && minLoad.trim() ? Number(minLoad) : null,
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
          {result.available ? (
            <>
              <p className="m-0 mb-1 text-sm">
                Band-averaged {result.band_averaged_pct?.toFixed(2)} % · pooled{" "}
                {result.pooled_pct?.toFixed(2)} % · n {result.n} · measured by{" "}
                <code>{result.measured}</code>
              </p>
              <PairingNote pairing={result.pairing} />
              <DelayStability sensitivity={result.delay_sensitivity} />
              <DeliveryChart result={result} />
            </>
          ) : (
            <EmptyResult
              kind={result.unavailable_kind}
              gates={result.gates}
              unavailable={result.unavailable}
            />
          )}
          <FloorNote floor={result.noise_floor} />
          <UnavailableList items={result.unavailable} />
          <ConditionsBlock
            conditions={result.conditions}
            extra={
              <>
                <dt>Measured</dt>
                <dd className="m-0">{result.measured}</dd>
                <dt>Load floor</dt>
                <dd className="m-0">{result.min_load ?? "none"}</dd>
              </>
            }
          />
        </>
      )}
    </Panel>
  );
}
