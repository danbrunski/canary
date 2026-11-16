import { useCallback, useEffect, useMemo, useState } from "react";

import { loadEditorState, saveEditorState } from "../lib/romEditor";
import type { PendingEdit, RomEdit } from "../types/romEditor";
import { useGeneration } from "./useGeneration";

/** The pending edits and label for one ROM image, and their persistence
 *  (R28.5): loaded from the browser's storage for `sha` on mount, saved on every
 *  change. `onEdited` runs after every change (add, remove, relabel), which is
 *  how a preview of the old edits is dropped: whoever shows a result of the
 *  edits passes its `invalidate` here, so a mutation that forgets to drop it
 *  has no way to be written. Keep it stable. */
export function useEditSession(sha: string, onEdited: () => void) {
  const [saved] = useState(() => loadEditorState(sha));
  const [pending, setPending] = useState<PendingEdit[]>(saved.pending);
  const [label, setLabelText] = useState(saved.label);
  const ids = useGeneration();
  const edits = useMemo<RomEdit[]>(() => pending.map((item) => item.edit), [pending]);

  useEffect(() => {
    saveEditorState(sha, { label, pending });
  }, [sha, label, pending]);

  const add = useCallback(
    (entry: Omit<PendingEdit, "id">) => {
      const id = `${Date.now()}-${ids.next()}`;
      setPending((items) => [...items, { id, ...entry }]);
      onEdited();
    },
    [ids, onEdited],
  );
  const remove = useCallback(
    (id: string) => {
      setPending((items) => items.filter((item) => item.id !== id));
      onEdited();
    },
    [onEdited],
  );
  const setLabel = useCallback(
    (value: string) => {
      setLabelText(value);
      onEdited();
    },
    [onEdited],
  );

  return { pending, edits, label, add, remove, setLabel };
}
