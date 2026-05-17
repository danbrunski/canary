import {
  panelClass,
  panelHeaderClass,
  panelTitleClass,
  placeholderClass,
} from "../styles";

interface PlaceholderPanelProps {
  title: string;
  /** What this panel will show, and which backend already serves it — so an
   *  unbuilt view is *informative*, not a dead "TODO". */
  summary: string;
  endpoint?: string;
  task: string;
}

/** Honest stand-in for a Phase-6 panel whose backend shipped but whose rich
 *  UI is a later task. The navigation refactor's job is to give every such
 *  panel a stable home now; this is what sits in that home until the real
 *  component lands. */
export function PlaceholderPanel({
  title,
  summary,
  endpoint,
  task,
}: PlaceholderPanelProps) {
  return (
    <section className={panelClass}>
      <header className={panelHeaderClass}>
        <h2 className={panelTitleClass}>{title}</h2>
        <span className="rounded-full border border-border-soft bg-black/25 px-2 py-0.5 text-[0.62rem] font-bold uppercase tracking-[0.1em] text-fg-faint">
          {task}
        </span>
      </header>
      <p className={placeholderClass}>{summary}</p>
      {endpoint && (
        <p className="mt-2 font-num text-[0.78rem] text-fg-dim">
          backend ready: <span className="text-accent-2">{endpoint}</span>
        </p>
      )}
    </section>
  );
}
