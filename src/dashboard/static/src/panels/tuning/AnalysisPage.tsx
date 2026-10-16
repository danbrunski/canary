import { useState } from "react";

import { SegmentedControl, type SegmentOption } from "../../components/SegmentedControl";
import type { TuningContext } from "../../hooks/useTuningContext";
import type { StatusResponse } from "../../types";
import type { SessionChoice } from "../../types/workbench";
import { DeliveryView } from "./DeliveryView";
import { DynoView } from "./DynoView";
import { SessionChooser } from "./SessionChooser";
import { TimingView } from "./TimingView";
import { TrimView } from "./TrimView";

// The views this page was split into (R19), still importable from here.
export { DeliveryChart } from "./DeliveryChart";
export { MEASURED_SOURCES } from "./DeliveryView";

type View = "delivery" | "trim" | "timing" | "dyno";
const VIEWS: ReadonlyArray<SegmentOption<View>> = [
  { id: "delivery", label: "Delivery error" },
  { id: "trim", label: "Trim surface" },
  { id: "timing", label: "Timing" },
  { id: "dyno", label: "Dyno" },
];

/** W04 Analysis: derived results that may pool many sessions. Each view
 *  shows n, its gate report and conditions, and says which kind of
 *  unavailable an empty result is. The page owns which sessions are chosen
 *  and which view is open; each view owns its own request. */
export function AnalysisPage({
  token,
  status,
  context,
}: {
  token: string;
  status: StatusResponse | null;
  context: TuningContext;
}) {
  const [choice, setChoice] = useState<SessionChoice>({ sessions: [], revision: null });
  const [view, setView] = useState<View>("delivery");
  const runningRevision = status?.registry_revision ?? null;
  return (
    <div className="flex flex-col gap-4">
      {!token && <p className="m-0 text-sm">Enter the control token to run analyses.</p>}
      <SessionChooser
        token={token}
        choice={choice}
        onChoice={setChoice}
        runningRevision={runningRevision}
        selectedLogId={context.selectedLogId}
      />
      <SegmentedControl label="Analysis views" options={VIEWS} value={view} onChange={setView} />
      {view === "delivery" && <DeliveryView token={token} choice={choice} />}
      {view === "trim" && <TrimView token={token} choice={choice} />}
      {view === "timing" && <TimingView token={token} choice={choice} />}
      {view === "dyno" && <DynoView token={token} choice={choice} />}
    </div>
  );
}
