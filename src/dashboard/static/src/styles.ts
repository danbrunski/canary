export function cn(...classes: Array<string | false | null | undefined>): string {
  return classes.filter(Boolean).join(" ");
}

export const shellClass = [
  "min-h-screen",
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

export const mainClass = "flex-1 p-[1.15rem]";

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
export const panelCountClass =
  "font-num text-[0.74rem] tracking-[0.03em] text-fg-faint";
export const placeholderClass = "m-0 text-fg-faint italic";
export const dimTextClass = "text-fg-dim";

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
export const definitionRowClass =
  "flex items-baseline justify-between gap-3 text-[0.88rem]";
export const definitionTermClass =
  "text-[0.72rem] uppercase tracking-[0.08em] text-fg-faint";
export const definitionValueClass = "m-0 font-num tabular-nums";

export function noticeClass(kind: "ok" | "error" | "busy"): string {
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
export const statusLabelClass =
  "text-[0.6rem] uppercase tracking-[0.16em] text-fg-faint";
export const statusValueClass = "font-num tabular-nums text-fg";

/** Workbench: sidebar on wide screens, horizontal scroller on phones. */
export const workbenchClass =
  "grid gap-[1.15rem] grid-cols-[13rem_1fr] max-[900px]:grid-cols-1";

export const sidebarClass =
  "flex flex-col gap-4 max-[900px]:flex-row max-[900px]:gap-2 max-[900px]:overflow-x-auto";

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

export function groupTabClass(active: boolean): string {
  return cn(
    "min-h-8 rounded-full border-0 bg-transparent px-3.5 py-1.5 text-[0.82rem]",
    "font-semibold tracking-[0.02em] text-fg-dim transition-colors hover:text-fg",
    "max-[900px]:flex-[1_0_auto]",
    active &&
      "bg-[linear-gradient(180deg,var(--color-elev-3),var(--color-elev-2))] text-fg shadow-[inset_0_1px_0_rgba(255,255,255,0.05),0_2px_8px_-4px_rgba(0,0,0,0.7)]",
  );
}

export const groupTabsClass =
  "flex items-center gap-1 rounded-full border border-border-soft bg-black/30 p-1 max-[900px]:w-full max-[900px]:overflow-x-auto";
