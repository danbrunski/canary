import { NAV } from "../nav";
import { areaBarClass, areaTabClass, cn } from "../styles";
import type { NavGroupId } from "../types";

/** The Workbench's top level (W10): one tab per NAV group, so the sidebar
 *  lists one area's pages rather than all of them. The settings group sits at
 *  the far end behind a gear. Choosing an area is the caller's to resolve to a
 *  page (the one last open there). */
export function AreaBar({
  active,
  onChoose,
}: {
  active: NavGroupId;
  onChoose: (group: NavGroupId) => void;
}) {
  return (
    <nav aria-label="Workbench areas" className={areaBarClass}>
      {NAV.map((group) => (
        <button
          key={group.id}
          type="button"
          className={cn(areaTabClass(group.id === active), group.settings && "ml-auto")}
          aria-current={group.id === active ? "page" : undefined}
          onClick={() => onChoose(group.id)}
        >
          {group.settings && <span aria-hidden="true">⚙</span>}
          {group.label}
        </button>
      ))}
    </nav>
  );
}
