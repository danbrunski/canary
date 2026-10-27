export function cn(...classes: Array<string | false | null | undefined>): string {
  return classes.filter(Boolean).join(" ");
}

/** The shell owns the viewport: it never scrolls itself, so the header and
 *  status strip stay put and the panes below scroll independently. */
export const shellClass = [
  "h-dvh",
  "overflow-hidden",
  "flex",
  "flex-col",
  "bg-bg",
  "text-fg",
  "antialiased",
  "[color-scheme:dark]",
  "[text-rendering:optimizeLegibility]",
  "bg-[radial-gradient(900px_600px_at_88%_-10%,rgba(79,214,224,0.06),transparent_60%),radial-gradient(1100px_700px_at_6%_-8%,rgba(255,181,71,0.05),transparent_55%),var(--color-bg)]",
  "bg-fixed",
  // viewport-fit=cover (index.html) hands the notch/rounded corners to the
  // page; the side insets are 0 everywhere but a landscape phone.
  "pl-[env(safe-area-inset-left)]",
  "pr-[env(safe-area-inset-right)]",
].join(" ");

/** Body region between the status strip and the viewport floor. It only
 *  distributes height — its children own the scrolling and the padding. */
export const mainClass = "flex min-h-0 flex-1 flex-col";

/** Single scroll pane for a context that has no sidebar (Drive).
 *
 *  This is a plain block, and the panel grid goes *inside* it. Do not merge
 *  the two: a grid that is itself the scroll container has a definite
 *  height, which sizes its auto row to the pane instead of to the content.
 *  Panels are `overflow-hidden` (panelClass), so an under-sized row makes a
 *  tall panel clip its own content while the scroll container still reports
 *  no overflow — content disappears past the fold with no scrollbar and no
 *  way to reach it. An indefinite-height inner grid overflows honestly. */
export const scrollPaneClass = "min-h-0 flex-1 overflow-y-auto p-[1.15rem] max-[600px]:p-3";

export const responsiveGridClass = "grid gap-[1.15rem] max-[900px]:grid-cols-1";

export const headerClass = [
  "sticky",
  "top-0",
  "z-[5]",
  "flex",
  "items-center",
  "gap-4",
  "border-b",
  "border-border-soft",
  "bg-[linear-gradient(180deg,rgba(20,26,33,0.92),rgba(15,19,24,0.86))]",
  "px-[1.15rem]",
  "py-2.5",
  "backdrop-blur-md",
  "max-[900px]:flex-wrap",
  "max-[900px]:items-stretch",
  // Phone header (P2.2): brand, connection status and one menu on one row,
  // clear of the status bar when the PWA draws under it.
  "max-[600px]:flex-nowrap",
  "max-[600px]:items-center",
  "max-[600px]:gap-2",
  "max-[600px]:px-3",
  "max-[600px]:pb-2",
  "max-[600px]:pt-[max(0.5rem,env(safe-area-inset-top))]",
].join(" ");

export const panelClass = [
  "relative",
  "overflow-hidden",
  "rounded-card",
  "border",
  "border-border-soft",
  "bg-[linear-gradient(180deg,var(--color-elev),var(--color-bg))]",
  "p-4",
  "max-[600px]:p-3",
  "shadow-panel",
  "before:absolute",
  "before:inset-x-0",
  "before:top-0",
  "before:h-0.5",
  "before:bg-[linear-gradient(90deg,transparent,var(--color-accent)_30%,var(--color-accent-2)_75%,transparent)]",
  "before:opacity-50",
].join(" ");

export const panelHeaderClass =
  "mb-3 flex items-baseline justify-between gap-4 max-[600px]:flex-wrap max-[600px]:gap-2";
export const panelHeaderCenterClass =
  "mb-3 flex items-center justify-between gap-4 max-[600px]:flex-wrap max-[600px]:gap-2";
export const panelTitleClass =
  "m-0 text-[0.82rem] font-bold uppercase tracking-[0.14em] text-fg-dim";
export const panelCountClass = "font-num text-[0.74rem] tracking-[0.03em] text-fg-faint";
export const placeholderClass = "m-0 text-fg-faint italic";

export const inputClass = [
  "min-h-9",
  "rounded-[8px]",
  "border",
  "border-border-soft",
  "bg-elev-2",
  "px-2.5",
  "py-1.5",
  "text-fg",
  "transition-[border-color,box-shadow]",
  "duration-150",
  "hover:border-border",
  "focus-visible:border-accent-2",
  "focus-visible:outline-none",
  "focus-visible:ring-2",
  "focus-visible:ring-accent-2/25",
].join(" ");

export const selectClass = inputClass;

export const buttonClass = [
  "min-h-9",
  "rounded-[8px]",
  "border",
  "border-border",
  "bg-elev-2",
  "px-3",
  "py-1.5",
  "text-fg",
  "transition-[background,color,border-color,box-shadow,transform]",
  "duration-150",
  "hover:border-fg-faint",
  "active:translate-y-px",
  "disabled:cursor-default",
  "disabled:text-fg-dim",
  "disabled:opacity-60",
  "focus-visible:outline-none",
  "focus-visible:ring-2",
  "focus-visible:ring-accent-2",
  "focus-visible:ring-offset-2",
  "focus-visible:ring-offset-bg",
].join(" ");

export const dangerButtonClass = cn(buttonClass, "border-bad text-bad");

export const badgeClass = [
  "inline-flex",
  "items-center",
  "gap-1",
  "rounded-full",
  "border",
  "border-border-soft",
  "bg-black/25",
  "px-2",
  "py-0.5",
  "text-[0.68rem]",
  "font-bold",
  "uppercase",
  "tracking-[0.06em]",
].join(" ");

export const definitionListClass = "m-0 flex flex-col gap-1.5";
/** A key/value row. Below 600px (P2.5) it becomes a small stacked card —
 *  term above value — so a long value (a storage path, a revision string)
 *  wraps under its label instead of squeezing it off the row. */
export const definitionRowClass =
  "flex items-baseline justify-between gap-3 text-[0.88rem] " +
  "max-[600px]:flex-col max-[600px]:items-stretch max-[600px]:gap-0.5 " +
  "max-[600px]:rounded-tile max-[600px]:border max-[600px]:border-border-soft " +
  "max-[600px]:bg-black/15 max-[600px]:px-2.5 max-[600px]:py-1.5";
export const definitionTermClass = "text-[0.72rem] uppercase tracking-[0.08em] text-fg-faint";
export const definitionValueClass =
  "m-0 min-w-0 break-words font-num tabular-nums max-[600px]:text-left";

/** The one wrapper for a wide data table (P2.5): the table scrolls sideways
 *  inside its own box instead of pushing the page wider, and below 600px its
 *  first column (the row's name) stays put while the rest scrolls under it.
 *  Use `TableScroll` (components/TableScroll) rather than this directly. */
export const tableScrollClass =
  "max-w-full overflow-x-auto overscroll-x-contain " +
  "max-[600px]:[&_tr>:first-child]:sticky max-[600px]:[&_tr>:first-child]:left-0 " +
  "max-[600px]:[&_tr>:first-child]:z-[1] max-[600px]:[&_tr>:first-child]:bg-elev";

/** A two-column key/value `<table>` that stacks into cards below 600px: each
 *  row becomes a block with the key over the value. */
export const keyValueTableClass =
  "w-full border-collapse " +
  "max-[600px]:block max-[600px]:[&_tbody]:flex max-[600px]:[&_tbody]:flex-col " +
  "max-[600px]:[&_tbody]:gap-1.5 max-[600px]:[&_tr]:flex max-[600px]:[&_tr]:flex-col " +
  "max-[600px]:[&_tr]:rounded-tile max-[600px]:[&_tr]:border " +
  "max-[600px]:[&_tr]:border-border-soft max-[600px]:[&_tr]:px-2.5 max-[600px]:[&_tr]:py-1.5 " +
  "max-[600px]:[&_td]:w-auto max-[600px]:[&_td]:p-0";

export type NoticeKind = "ok" | "error" | "busy";

/** The notice state shape shared by every action panel. */
export interface NoticeState {
  kind: NoticeKind;
  text: string;
}

export function noticeClass(kind: NoticeKind): string {
  return cn(
    "rounded-[8px] border bg-elev px-3 py-2 text-[0.86rem] text-fg",
    kind === "ok" && "border-ok/45",
    kind === "error" && "border-bad/55 text-bad",
    kind === "busy" && "border-accent/55 text-accent",
  );
}

// ---------------------------------------------------------------------------
// Navigation refactor (Phase A): mode-driven shell, grouped Workbench nav,
// persistent status strip.
// ---------------------------------------------------------------------------

/** Status spine under the header — the always-visible "is everything OK". */
export const statusStripClass = [
  "flex",
  "flex-wrap",
  "items-center",
  "gap-x-5",
  "gap-y-1.5",
  "border-b",
  "border-border-soft",
  "bg-black/20",
  "px-[1.15rem]",
  "py-2",
  "text-[0.74rem]",
  "text-fg-dim",
].join(" ");

export const statusItemClass = "inline-flex items-center gap-1.5";
export const statusLabelClass = "text-[0.6rem] uppercase tracking-[0.16em] text-fg-faint";
export const statusValueClass = "font-num tabular-nums text-fg";

/** Workbench: the area bar (W10), then the sidebar and the panel column. */
export const workbenchClass = "flex min-h-0 flex-1 flex-col";

/** The area bar: one tab per NAV group, the settings group at its far end. */
export const areaBarClass =
  "flex shrink-0 items-stretch gap-1 overflow-x-auto border-b border-border-soft px-[1.15rem]";

export function areaTabClass(active: boolean): string {
  return cn(
    "relative inline-flex min-h-10 shrink-0 items-center gap-1.5 whitespace-nowrap px-3",
    "text-[0.85rem] font-semibold text-fg-dim transition-colors hover:text-fg",
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent-2",
    active &&
      "text-accent after:absolute after:inset-x-2 after:bottom-0 after:h-0.5 after:rounded-full after:bg-accent",
  );
}

/** Below the area bar: sidebar on wide screens, horizontal scroller on phones.
 *  Wide: the grid fills the shell's remaining height and each column
 *  scrolls on its own, so a long panel never pushes the nav out of reach.
 *  Collapsed (W10.4), the sidebar is a rail holding its expand button.
 *  Below 900px it collapses to one column and the grid itself scrolls —
 *  nested scroll regions on a phone are worse than a single one. There the
 *  strip's row is `max-content`: the strip scrolls sideways, which makes it
 *  a scroll container, and an `auto` row holding one may shrink to nothing.
 *  On a tall page the row measured 17px, padding only, the pages spilling
 *  under the panel below it (W10). */
export function workbenchBodyClass(collapsed: boolean): string {
  return cn(
    "grid min-h-0 flex-1 max-[900px]:grid-cols-1 max-[900px]:grid-rows-[max-content_auto] " +
      "max-[900px]:overflow-y-auto",
    collapsed ? "grid-cols-[2.75rem_1fr]" : "grid-cols-[13rem_1fr]",
  );
}

export const sidebarClass =
  "flex flex-col gap-1 overflow-y-auto overscroll-contain border-r border-border-soft px-3 py-[1.15rem] " +
  "max-[900px]:flex-row max-[900px]:gap-2 max-[900px]:overflow-x-auto max-[900px]:overflow-y-visible " +
  "max-[900px]:border-r-0 max-[900px]:border-b max-[900px]:px-[1.15rem] max-[900px]:py-2";

/** The panel column beside the sidebar — the other independent scroll pane.
 *  A plain block wrapping the panel grid, for the reason in
 *  `scrollPaneClass` above. */
export const workbenchContentClass =
  "min-w-0 overflow-y-auto overscroll-contain p-[1.15rem] max-[900px]:overflow-y-visible";

/** The sidebar's collapse / expand toggle; the strip below 900px has none. */
export const sidebarToggleClass =
  "mt-auto min-h-9 rounded-[8px] px-2 text-left text-[0.8rem] text-fg-faint transition-colors " +
  "hover:text-fg max-[900px]:hidden";

export function navItemClass(active: boolean): string {
  return cn(
    "min-h-9 rounded-[8px] border border-transparent px-3 py-1.5 text-left text-[0.85rem]",
    "font-medium text-fg-dim transition-colors hover:text-fg",
    "max-[900px]:whitespace-nowrap",
    active &&
      "border-border-soft bg-[linear-gradient(180deg,var(--color-elev-3),var(--color-elev-2))] text-fg",
  );
}

// ---------------------------------------------------------------------------
// Phone layout (docs/roadmap/phone-ui.md P2.2). Below 600px the sidebar and
// the header's widget row give way to a bottom bar of five entries, a "More"
// sheet holding the rest of NAV, and one header menu.
// ---------------------------------------------------------------------------

/** Workbench body on a phone: an optional page strip, then one scroll pane. */
export const phoneWorkbenchClass = "flex min-h-0 flex-1 flex-col";

/** The bottom bar is the shell's last flex row, not `position: fixed`: the
 *  panes above already own the scrolling, so nothing ever slides under it
 *  and no pane needs bottom padding to stay reachable. */
export const bottomBarClass = [
  "grid",
  "shrink-0",
  "border-t",
  "border-border-soft",
  "bg-elev",
  "pb-[env(safe-area-inset-bottom)]",
].join(" ");

export function bottomTabClass(active: boolean): string {
  return cn(
    "relative flex min-h-14 min-w-0 flex-col items-center justify-center px-1",
    "text-[0.7rem] font-semibold uppercase tracking-[0.05em] text-fg-dim",
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent-2",
    active &&
      "text-accent before:absolute before:inset-x-3 before:top-0 before:h-0.5 before:rounded-full before:bg-accent",
  );
}

/** Backdrop + bottom sheet shared by the More sheet and the header menu. */
export const sheetBackdropClass = "fixed inset-0 z-20 bg-black/55";
export const sheetClass = [
  "fixed",
  "inset-x-0",
  "bottom-0",
  "z-30",
  "flex",
  "max-h-[80dvh]",
  "flex-col",
  "rounded-t-[16px]",
  "border-t",
  "border-border",
  "bg-elev",
  "shadow-panel",
  "pb-[env(safe-area-inset-bottom)]",
  "pl-[env(safe-area-inset-left)]",
  "pr-[env(safe-area-inset-right)]",
].join(" ");
export const sheetHeaderClass = "flex shrink-0 items-center justify-between gap-3 px-4 py-3";
export const sheetBodyClass = "min-h-0 overflow-y-auto overscroll-contain px-4 pb-4";

export function sheetItemClass(active: boolean): string {
  return cn(
    "flex min-h-11 w-full items-center rounded-[8px] px-3 text-left text-[0.95rem] text-fg-dim",
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-2",
    active && "bg-elev-3 text-fg",
  );
}

/** The sibling pages under one bottom-bar entry (e.g. Alerts: codes,
 *  anomalies, alarms): a strip of chips above the pane. */
export const pageStripClass =
  "flex shrink-0 gap-2 overflow-x-auto border-b border-border-soft px-3 py-2";

export function pageChipClass(active: boolean): string {
  return cn(
    "min-h-9 shrink-0 whitespace-nowrap rounded-full border px-3 text-[0.8rem]",
    active ? "border-accent bg-accent/15 text-accent" : "border-border-soft text-fg-dim",
  );
}
