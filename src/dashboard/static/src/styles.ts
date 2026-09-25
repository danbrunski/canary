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
export const scrollPaneClass = "min-h-0 flex-1 overflow-y-auto p-[1.15rem]";

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
].join(" ");

export const panelClass = [
  "relative",
  "overflow-hidden",
  "rounded-card",
  "border",
  "border-border-soft",
  "bg-[linear-gradient(180deg,var(--color-elev),var(--color-bg))]",
  "p-4",
  "shadow-panel",
  "before:absolute",
  "before:inset-x-0",
  "before:top-0",
  "before:h-0.5",
  "before:bg-[linear-gradient(90deg,transparent,var(--color-accent)_30%,var(--color-accent-2)_75%,transparent)]",
  "before:opacity-50",
].join(" ");

export const panelHeaderClass = "mb-3 flex items-baseline justify-between gap-4";
export const panelHeaderCenterClass = "mb-3 flex items-center justify-between gap-4";
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
export const definitionRowClass = "flex items-baseline justify-between gap-3 text-[0.88rem]";
export const definitionTermClass = "text-[0.72rem] uppercase tracking-[0.08em] text-fg-faint";
export const definitionValueClass = "m-0 font-num tabular-nums";

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

/** Workbench: sidebar on wide screens, horizontal scroller on phones.
 *  Wide: the grid fills the shell's remaining height and each column
 *  scrolls on its own, so a long panel never pushes the nav out of reach.
 *  Below 900px it collapses to one column and the grid itself scrolls —
 *  nested scroll regions on a phone are worse than a single one. */
export const workbenchClass =
  "grid min-h-0 flex-1 grid-cols-[13rem_1fr] max-[900px]:grid-cols-1 " +
  "max-[900px]:overflow-y-auto";

export const sidebarClass =
  "flex flex-col gap-4 overflow-y-auto overscroll-contain border-r border-border-soft px-3 py-[1.15rem] " +
  "max-[900px]:flex-row max-[900px]:gap-2 max-[900px]:overflow-x-auto max-[900px]:overflow-y-visible " +
  "max-[900px]:border-r-0 max-[900px]:border-b max-[900px]:px-[1.15rem] max-[900px]:py-2";

/** The panel column beside the sidebar — the other independent scroll pane.
 *  A plain block wrapping the panel grid, for the reason in
 *  `scrollPaneClass` above. */
export const workbenchContentClass =
  "min-w-0 overflow-y-auto overscroll-contain p-[1.15rem] " + "max-[900px]:overflow-y-visible";

export const navGroupLabelClass =
  "px-2 text-[0.6rem] font-bold uppercase tracking-[0.16em] text-fg-faint max-[900px]:hidden";

export function navItemClass(active: boolean): string {
  return cn(
    "min-h-9 rounded-[8px] border border-transparent px-3 py-1.5 text-left text-[0.85rem]",
    "font-medium text-fg-dim transition-colors hover:text-fg",
    "max-[900px]:whitespace-nowrap",
    active &&
      "border-border-soft bg-[linear-gradient(180deg,var(--color-elev-3),var(--color-elev-2))] text-fg",
  );
}
