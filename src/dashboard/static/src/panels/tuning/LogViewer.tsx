import { useCallback, useMemo, useState } from "react";

import { getFrame } from "../../api/workbench";
import { Panel } from "../../components/Panel";
import { SegmentedControl, type SegmentOption } from "../../components/SegmentedControl";
import { useControlResource } from "../../hooks/useControlResource";
import { useLocalStorageJson } from "../../hooks/useLocalStorage";
import { REGIMES, describeRefusal } from "../../lib/workbenchApi";
import { buttonClass, inputClass, selectClass } from "../../styles";
import type { Regime } from "../../types/workbench";
import { FrameNotes } from "./FrameNotes";
import { FrameTable } from "./FrameTable";
import { FrameVisual } from "./FrameVisual";
import { RefusalNotice } from "./WorkbenchNotices";

/** J1979 names every OBD-II vehicle answers — a starting set, not a vehicle
 *  fact. The operator edits it; the choice persists. */
const DEFAULT_SPECS = "obd:RPM,obd:MAF,obd:COMMANDED_EQUIV_RATIO,obd:O2_WR_LAMBDA_S1";
const SPECS_KEY = "canary.tuning.logSpecs.v1";

type Presentation = "table" | "visual";
const PRESENTATIONS: ReadonlyArray<SegmentOption<Presentation>> = [
  { id: "table", label: "Table" },
  { id: "visual", label: "Visual" },
];

/** One log: the channel/grid/regime controls, the request for its frame, and
 *  the frame in a table or visual presentation. Owns the request identity
 *  (token, log, applied channels, grid, regime, page offset). */
export function LogViewer({ token, sessionId }: { token: string; sessionId: string }) {
  const [specsText, setSpecsText] = useLocalStorageJson(SPECS_KEY, DEFAULT_SPECS);
  const [draft, setDraft] = useState(specsText);
  const [grid, setGrid] = useState("1s");
  const [regime, setRegime] = useState<Regime>("all");
  const [presentation, setPresentation] = useState<Presentation>("table");

  const specs = useMemo(
    () =>
      specsText
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
    [specsText],
  );

  // The page offset belongs to one view: a new log, channel set, grid or
  // regime starts again at row 0 (derived, not reset in an effect, so the
  // stale offset is never requested).
  const viewKey = `${sessionId}|${specsText}|${grid}|${regime}`;
  const [page, setPage] = useState({ key: viewKey, offset: 0 });
  const offset = page.key === viewKey ? page.offset : 0;
  const setOffset = (next: number) => setPage({ key: viewKey, offset: next });

  const fetchFrame = useCallback(
    (signal: AbortSignal) => getFrame(token, sessionId, { specs, grid, regime, offset }, signal),
    [token, sessionId, specs, grid, regime, offset],
  );
  const {
    data,
    error: refusal,
    loading: first,
    stale,
  } = useControlResource(fetchFrame, {
    enabled: Boolean(token) && specs.length > 0,
    intervalMs: null,
    describe: describeRefusal,
  });
  const loading = first || stale;
  // A refusal replaces the view; the previous frame is not shown under it.
  const frame = refusal ? null : data;

  return (
    <Panel
      title={sessionId}
      actions={
        <SegmentedControl
          label="Presentation"
          options={PRESENTATIONS}
          value={presentation}
          onChange={setPresentation}
        />
      }
    >
      <form
        className="mb-3 flex flex-wrap items-end gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          setSpecsText(draft);
        }}
      >
        <label className="flex min-w-72 flex-1 flex-col gap-1 text-xs">
          Channels (comma-separated specs)
          <input className={inputClass} value={draft} onChange={(e) => setDraft(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1 text-xs">
          Grid
          <select className={selectClass} value={grid} onChange={(e) => setGrid(e.target.value)}>
            {["100ms", "250ms", "500ms", "1s", "5s"].map((g) => (
              <option key={g}>{g}</option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs">
          Regime
          <select
            className={selectClass}
            aria-label="Regime"
            value={regime}
            onChange={(e) => setRegime(e.target.value as Regime)}
          >
            {REGIMES.map((r) => (
              <option key={r.id} value={r.id}>
                {r.label}
              </option>
            ))}
          </select>
        </label>
        <button type="submit" className={buttonClass}>
          Apply
        </button>
      </form>
      <RefusalNotice refusal={refusal} />
      {loading && <p className="m-0 text-xs text-fg-faint">Loading…</p>}
      {frame && <FrameNotes frame={frame} onOffset={setOffset} />}
      {frame && presentation === "table" && <FrameTable frame={frame} />}
      {frame && presentation === "visual" && <FrameVisual frame={frame} />}
    </Panel>
  );
}
