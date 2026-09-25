import type { ReactNode } from "react";

import {
  cn,
  panelClass,
  panelCountClass,
  panelHeaderCenterClass,
  panelHeaderClass,
  panelTitleClass,
} from "../styles";

interface PanelProps {
  title: string;
  /** Right-aligned secondary text (counts, etc.). Ignored when `actions`
   *  is given. */
  count?: ReactNode;
  /** Right-aligned header content (filters, toggles). Overrides `count`. */
  actions?: ReactNode;
  /** Vertically center the header row (use when `actions` has controls
   *  taller than the title). */
  centerHeader?: boolean;
  /** Extra classes on the panel `<section>` (e.g. an accent tint). */
  className?: string;
  children: ReactNode;
}

/** The standard panel shell: the gradient card + accent hairline + the
 *  title/header row, in one place. Panels stop hand-wrapping `panelClass`
 *  + a `<header>` + `<h2>`; this is also the unit the 6.9 dashboard
 *  builder composes. The rendered structure (a `<section>` with an
 *  `<h2>{title}</h2>`) is unchanged, so existing role/text-based tests
 *  keep passing. */
export function Panel({
  title,
  count,
  actions,
  centerHeader = false,
  className,
  children,
}: PanelProps) {
  return (
    <section className={cn(panelClass, className)}>
      <header className={centerHeader ? panelHeaderCenterClass : panelHeaderClass}>
        <h2 className={panelTitleClass}>{title}</h2>
        {/* count + actions coexist (count = info, actions = controls);
            either may be omitted. */}
        {(count != null || actions) && (
          <span className="flex items-center gap-3">
            {count != null && <span className={panelCountClass}>{count}</span>}
            {actions}
          </span>
        )}
      </header>
      {children}
    </section>
  );
}

type GridCols = 1 | 2 | 3;

const COLS: Record<GridCols, string> = {
  1: "grid-cols-1",
  2: "grid-cols-2 max-[1100px]:grid-cols-1",
  3: "grid-cols-3 max-[1100px]:grid-cols-1",
};

/** Responsive panel grid. A thin, named wrapper over the shared grid class
 *  so layouts declare intent (`cols={2}`) instead of repeating utility
 *  strings — the seam the 6.9 custom-dashboard builder will configure. */
export function PanelGrid({ cols = 1, children }: { cols?: GridCols; children: ReactNode }) {
  return (
    <div className={cn("grid gap-[1.15rem] max-[900px]:grid-cols-1", COLS[cols])}>{children}</div>
  );
}
