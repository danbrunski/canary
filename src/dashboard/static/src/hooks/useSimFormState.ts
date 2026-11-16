import { useCallback, useMemo, useReducer, useRef } from "react";

import { getRomTypicalValues } from "../api/workbench";
import { forgetRemembered, loadRemembered, suggestEdges, type FormKey } from "../lib/romSim";
import { initSimForm, simFormReducer } from "../lib/romSimForm";
import {
  fineReference,
  presetFill,
  simFormReadout,
  type SimFormReadout,
  type SimPreset,
  type TimedRun,
} from "../lib/romSimSize";
import { typicalFailure } from "../lib/romSimTypical";
import type { RomEdit, RomTypicalValues } from "../types/romEditor";
import type { RomTableValues } from "../types/romLibrary";
import { useLatestRequest } from "./useGeneration";

/** The Simulate form's state (R28.5): the reducer of `lib/romSimForm`, the
 *  storage it reads and writes (the remembered values), the readout the view
 *  shows (`simFormReadout`), and the typical-values fetch, held so that the
 *  click, the cache and the answer arriving after an edit cannot disagree.
 *
 *  `token` and `sha` are for the typical-values request only: the base image's
 *  vehicle's typical values are the ones offered. */
export function useSimFormState({
  token,
  sha,
  tables,
  label,
  edits,
  timings,
}: {
  token: string;
  sha: string;
  tables: RomTableValues[];
  label: string;
  edits: RomEdit[];
  /** This server's earlier runs (`romSimSize.loadTimings`), for the estimate. */
  timings: TimedRun[];
}) {
  const suggestion = useMemo(() => suggestEdges(tables), [tables]);
  const [state, dispatch] = useReducer(simFormReducer, undefined, () =>
    initSimForm(suggestion, loadRemembered()),
  );
  const fine = useMemo(() => fineReference(suggestion), [suggestion]);
  const fills = useMemo(
    () => ({ coarse: presetFill("coarse", suggestion), fine: presetFill("fine", suggestion) }),
    [suggestion],
  );
  const readout: SimFormReadout = useMemo(
    () =>
      simFormReadout({
        values: state.values,
        label,
        edits,
        tables,
        suggestion,
        fine,
        applied: state.applied,
        timings,
      }),
    [state.values, state.applied, label, edits, tables, suggestion, fine, timings],
  );

  // "Fill typical values" (W09.11): the response is fetched once per form and
  // kept; a click is an explicit act on this page, never a remembered default.
  const typicalCache = useRef<RomTypicalValues | null>(null);
  const typicalRequest = useLatestRequest();
  const typicalKind = state.typicalState.kind;
  const fillTypical = useCallback(async () => {
    if (typicalKind === "loading") return;
    if (typicalCache.current) {
      dispatch({ type: "typicalFilled", response: typicalCache.current });
      return;
    }
    const { token: mine, signal } = typicalRequest.start();
    dispatch({ type: "typicalLoading" });
    try {
      const response = await getRomTypicalValues(token, sha, signal);
      if (!typicalRequest.isCurrent(mine)) return;
      typicalCache.current = response;
      dispatch({ type: "typicalFilled", response });
    } catch (err) {
      if (!typicalRequest.isCurrent(mine)) return;
      dispatch({ type: "typicalFailed", failure: typicalFailure(err) });
    }
  }, [typicalKind, typicalRequest, token, sha]);

  const change = useCallback(
    (key: FormKey, value: string) => dispatch({ type: "change", key, value }),
    [],
  );
  const applyPreset = useCallback(
    (preset: SimPreset) => {
      const fill = fills[preset];
      if (fill) dispatch({ type: "preset", preset, fill });
    },
    [fills],
  );
  const resetEdges = useCallback(() => {
    dispatch({
      type: "resetEdges",
      rpmEdges: suggestion.rpmEdges ?? [],
      loadEdges: suggestion.loadEdges ?? [],
    });
  }, [suggestion]);
  const clearRemembered = useCallback(() => {
    forgetRemembered();
    dispatch({ type: "clearRemembered" });
  }, []);

  return {
    state,
    suggestion,
    fills,
    readout,
    change,
    applyPreset,
    resetEdges,
    clearRemembered,
    fillTypical,
  };
}

export type SimFormHandle = ReturnType<typeof useSimFormState>;
