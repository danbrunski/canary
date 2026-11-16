import { useMemo, useState } from "react";

import { simulateRomEdits } from "../api/workbench";
import { editsTouchWotColumn, gridPoints, simFailure, suggestEdges } from "../lib/romSim";
import { expectedRun, fineReference, loadTimings, recordTiming } from "../lib/romSimSize";
import type {
  PendingEdit,
  RomEdit,
  RomSimRequest,
  RomSimResult,
  SimFailure,
  SimRunContext,
} from "../types/romEditor";
import type { RomLibraryEntry, RomTableValues } from "../types/romLibrary";
import { useLatestRequest } from "./useGeneration";

/** What a run in flight is expected to take (the Cancel wording). */
interface Expected {
  seconds: number;
  measured: boolean;
}

export type SimState =
  | { kind: "idle" }
  | { kind: "running"; startedAt: number; expected: Expected }
  /** The page stopped waiting; the daemon did not stop. */
  | { kind: "cancelled"; startedAt: number; cancelledAt: number; expected: Expected }
  | { kind: "ok"; result: RomSimResult; run: SimRunContext }
  | { kind: "failed"; failure: SimFailure };

/** The Simulate run of one ROM image (R28.5): one request in flight, its state,
 *  the browser-kept timings the cost estimate is built from, and the run that
 *  was cancelled (so a later `analysis_busy` can be explained as that run).
 *  Leaving the image drops the wait; the daemon's job is its own. */
export function useSimRun({
  token,
  sha,
  entry,
  baseTables,
  pending,
  edits,
}: {
  token: string;
  sha: string;
  entry: RomLibraryEntry;
  baseTables: RomTableValues[];
  pending: PendingEdit[];
  /** The pending edits as the request carries them, for `stale`. */
  edits: RomEdit[];
}) {
  const [sim, setSim] = useState<SimState>({ kind: "idle" });
  // This server's earlier runs, kept by the browser: the cost estimate's basis.
  const [timings, setTimings] = useState(loadTimings);
  // A run this page stopped waiting for, so a later `analysis_busy` can be
  // explained as that run. Cleared by the next run that returns.
  const [abandoned, setAbandoned] = useState<{ startedAt: number; expectedS: number } | null>(null);
  const request = useLatestRequest();

  async function run(simRequest: RomSimRequest) {
    const { token: mine, signal } = request.start();
    const startedAt = Date.now();
    const points = gridPoints(simRequest.rpm_edges, simRequest.load_edges);
    const expected = expectedRun(points, simRequest.draws, timings);
    setSim({ kind: "running", startedAt, expected });
    try {
      const result = await simulateRomEdits(token, sha, simRequest, signal);
      if (!request.isCurrent(mine)) return;
      // This server's own wall time for this size: the next estimate's basis.
      recordTiming({ points, draws: simRequest.draws, seconds: (Date.now() - startedAt) / 1000 });
      setTimings(loadTimings());
      setAbandoned(null);
      setSim({
        kind: "ok",
        result,
        run: {
          request: simRequest,
          edits: pending.map((item) => ({
            tableName: item.tableName,
            summary: item.summary,
            chain: item.chain,
          })),
          base: {
            sha256: sha,
            label: entry.label ?? entry.filename ?? sha.slice(0, 12),
            revision: entry.revision_match.revision,
          },
          wotColumnEdited: editsTouchWotColumn(simRequest.edits, baseTables),
          elapsedS: Math.round((Date.now() - startedAt) / 1000),
          fineReference: fineReference(suggestEdges(baseTables)),
        },
      });
    } catch (err) {
      if (!request.isCurrent(mine)) return;
      setSim({ kind: "failed", failure: simFailure(err) });
    }
  }

  function cancel() {
    if (sim.kind !== "running") return;
    // Drop the wait (the result, if it ever comes, is not shown); the daemon's
    // job is its own and no route stops it.
    request.cancel();
    setAbandoned({ startedAt: sim.startedAt, expectedS: sim.expected.seconds });
    setSim({
      kind: "cancelled",
      startedAt: sim.startedAt,
      cancelledAt: Date.now(),
      expected: sim.expected,
    });
  }

  /** The shown result was for other edits than the pending ones now. */
  const stale = useMemo(
    () => sim.kind === "ok" && JSON.stringify(sim.run.request.edits) !== JSON.stringify(edits),
    [sim, edits],
  );

  return { sim, timings, abandoned, stale, run, cancel };
}
