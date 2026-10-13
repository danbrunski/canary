import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { cn, sheetBackdropClass, sheetBodyClass, sheetClass, sheetHeaderClass } from "../styles";

interface SheetProps {
  /** The dialog's accessible name and visible heading. */
  title: string;
  onClose: () => void;
  children: ReactNode;
}

/** A bottom sheet over a dimmed backdrop (phone layout, P2.2): the More
 *  sheet and the header menu. It closes on Escape, on a tap on the
 *  backdrop and on its close button; the caller closes it on a selection.
 *  Focus moves into the sheet on open and back to whatever opened it on
 *  close.
 *
 *  Portalled to `<body>`: the header's `backdrop-filter` makes it the
 *  containing block for `position: fixed` descendants, so a sheet rendered
 *  inside it would be pinned to the header instead of the viewport. */
export function Sheet({ title, onClose, children }: SheetProps) {
  const panel = useRef<HTMLDivElement>(null);
  // The listener is installed once; a parent re-rendering with a new
  // `onClose` must not re-run the effect and steal focus back to the panel.
  const close = useRef(onClose);
  useEffect(() => {
    close.current = onClose;
  }, [onClose]);

  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    panel.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      close.current();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      opener?.focus();
    };
  }, []);

  return createPortal(
    <>
      <div className={sheetBackdropClass} data-testid="sheet-backdrop" onClick={onClose} />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        className={cn(sheetClass, "text-fg outline-none")}
      >
        <div className={sheetHeaderClass}>
          <h2 className="m-0 text-[0.82rem] font-bold uppercase tracking-[0.14em] text-fg-dim">
            {title}
          </h2>
          <button
            type="button"
            className="min-h-11 min-w-11 rounded-[8px] text-[1.1rem] text-fg-dim"
            aria-label={`close ${title}`}
            onClick={onClose}
          >
            ✕
          </button>
        </div>
        <div className={sheetBodyClass}>{children}</div>
      </div>
    </>,
    document.body,
  );
}
