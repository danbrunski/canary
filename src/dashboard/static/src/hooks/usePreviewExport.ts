import { useCallback, useState } from "react";

import { downloadRomImage, exportRomEdits, previewRomEdits } from "../api/workbench";
import { previewRefusal } from "../lib/romEditor";
import type {
  RomEdit,
  RomEditExportResponse,
  RomEditPreview,
  RomEditRefusal,
} from "../types/romEditor";
import { downloadBlob } from "../util/browser";
import { downloadName } from "../lib/romLibrary";
import { useGeneration } from "./useGeneration";
import { describeError } from "./useControlResource";

export type PreviewState =
  | { kind: "idle" }
  | { kind: "busy" }
  | { kind: "ok"; data: RomEditPreview }
  | { kind: "refused"; refusal: RomEditRefusal }
  | { kind: "error"; text: string };

/** The export's state, held by the page so a new edit resets it. */
export type ExportState =
  | { kind: "idle" }
  | { kind: "busy" }
  | { kind: "ok"; data: RomEditExportResponse }
  | { kind: "refused"; refusal: RomEditRefusal }
  | { kind: "error"; text: string };

/** Preview and export of the pending edits (R28.5), under one request
 *  generation: any new edit calls `invalidate`, which drops both results and any
 *  request still in flight, so what is exported is what was previewed. The
 *  edits and label are arguments of the call, not state here: they belong to
 *  `useEditSession`, which takes this hook's `invalidate`. */
export function usePreviewExport(token: string, sha: string) {
  const generation = useGeneration();
  const [preview, setPreview] = useState<PreviewState>({ kind: "idle" });
  const [exported, setExported] = useState<ExportState>({ kind: "idle" });

  const invalidate = useCallback(() => {
    generation.bump();
    setPreview({ kind: "idle" });
    setExported({ kind: "idle" });
  }, [generation]);

  async function runPreview(label: string, edits: RomEdit[]) {
    const mine = generation.next();
    setPreview({ kind: "busy" });
    try {
      const data = await previewRomEdits(token, sha, { label: label.trim(), edits });
      if (generation.isCurrent(mine)) setPreview({ kind: "ok", data });
    } catch (err) {
      if (!generation.isCurrent(mine)) return;
      const refusal = previewRefusal(err);
      setPreview(
        refusal ? { kind: "refused", refusal } : { kind: "error", text: describeError(err) },
      );
    }
  }

  /** The previewed edits as a checksummed library entry. The button only
   *  shows under a preview, and any new edit clears both, so what is exported
   *  is what was previewed. */
  async function runExport(label: string, edits: RomEdit[]) {
    const mine = generation.current();
    setExported({ kind: "busy" });
    try {
      const data = await exportRomEdits(token, sha, { label: label.trim(), edits });
      if (generation.isCurrent(mine)) setExported({ kind: "ok", data });
    } catch (err) {
      if (!generation.isCurrent(mine)) return;
      const refusal = previewRefusal(err);
      setExported(
        refusal ? { kind: "refused", refusal } : { kind: "error", text: describeError(err) },
      );
    }
  }

  async function downloadExport(exportSha: string) {
    if (exported.kind !== "ok") return;
    downloadBlob(await downloadRomImage(token, exportSha), downloadName(exported.data.entry));
  }

  return { preview, exported, invalidate, runPreview, runExport, downloadExport };
}
