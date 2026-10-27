import { useEffect, useRef, useState } from "react";

import { buttonClass, cn } from "../../../styles";
import type { RomLibraryEntry } from "../../../types/romLibrary";
import { BasePicker } from "./BasePicker";

/** The loaded page's Recent ▾ (W10.8): the library's images newest first, in
 *  the same table the empty page shows, behind a button. Choosing one, Escape
 *  or a click elsewhere closes it. */
export function RecentMenu({
  entries,
  selectedSha,
  onChoose,
}: {
  entries: RomLibraryEntry[];
  selectedSha: string | null;
  onChoose: (sha: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const away = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  return (
    <div ref={root} className="relative">
      <button
        type="button"
        className={cn(buttonClass, open && "border-accent-2")}
        aria-expanded={open}
        aria-haspopup="dialog"
        disabled={entries.length === 0}
        onClick={() => setOpen(!open)}
      >
        Recent <span aria-hidden="true">▾</span>
      </button>
      {open && (
        <div
          role="dialog"
          aria-label="Recent ROMs"
          className="absolute right-0 top-full z-10 mt-2 w-[min(46rem,calc(100vw-2rem))] rounded-lg border border-border bg-elev p-3 shadow-lg"
        >
          <BasePicker
            entries={entries}
            selectedSha={selectedSha}
            onChoose={(sha) => {
              setOpen(false);
              onChoose(sha);
            }}
          />
        </div>
      )}
    </div>
  );
}
