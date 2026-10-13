import { useCallback, useState } from "react";

import { PHONE_TABS, moreNavGroups, phoneTabFor } from "../nav";
import { bottomBarClass, bottomTabClass, cn, sheetItemClass } from "../styles";
import type { AppMode, NavGroupDef, WorkbenchItem } from "../types";
import { Sheet } from "./Sheet";

interface PhoneNavProps {
  mode: AppMode;
  item: WorkbenchItem;
  /** Switch to the driving context (`#drive`). */
  onDrive: () => void;
  /** Open a Workbench page (`#wb/<item>`), leaving the driving context. */
  onNavigate: (item: WorkbenchItem) => void;
  /** The More sheet's contents; defaults to NAV minus the bottom-bar pages,
   *  so a page added to NAV appears here without touching this file. */
  groups?: readonly NavGroupDef[];
}

/** Phone navigation (P2.2): the five bottom-bar entries plus "More", which
 *  opens a sheet with the rest of NAV under its group labels. Routing is
 *  still the URL hash (useHashRoute); this only calls the setters. */
export function PhoneNav({ mode, item, onDrive, onNavigate, groups }: PhoneNavProps) {
  const [moreOpen, setMoreOpen] = useState(false);
  const closeMore = useCallback(() => setMoreOpen(false), []);
  const active = phoneTabFor(mode, item);
  const sheetGroups = groups ?? moreNavGroups();

  return (
    <>
      <nav
        aria-label="Phone sections"
        className={bottomBarClass}
        style={{ gridTemplateColumns: `repeat(${PHONE_TABS.length + 1}, minmax(0, 1fr))` }}
      >
        {PHONE_TABS.map((tab) => (
          <button
            key={tab.id}
            type="button"
            className={bottomTabClass(active?.id === tab.id)}
            aria-current={active?.id === tab.id ? "page" : undefined}
            onClick={() => (tab.items.length === 0 ? onDrive() : onNavigate(tab.items[0]))}
          >
            {tab.label}
          </button>
        ))}
        <button
          type="button"
          className={bottomTabClass(active === null || moreOpen)}
          aria-haspopup="dialog"
          aria-expanded={moreOpen}
          aria-current={active === null ? "page" : undefined}
          onClick={() => setMoreOpen(true)}
        >
          More
        </button>
      </nav>
      {moreOpen && (
        <Sheet title="More" onClose={closeMore}>
          <div className="flex flex-col gap-4">
            {sheetGroups.map((group) => (
              <section key={group.id} aria-label={group.label} className="flex flex-col gap-0.5">
                <h3 className="m-0 px-3 pb-1 text-[0.62rem] font-bold uppercase tracking-[0.16em] text-fg-faint">
                  {group.label}
                </h3>
                {group.items.map((entry) => {
                  const current = mode === "parked" && entry.id === item;
                  return (
                    <button
                      key={entry.id}
                      type="button"
                      className={cn(sheetItemClass(current), entry.planned && "text-fg-faint")}
                      aria-current={current ? "page" : undefined}
                      onClick={() => {
                        onNavigate(entry.id);
                        closeMore();
                      }}
                    >
                      {entry.label}
                      {entry.placeholder && (
                        <span className="ml-1.5 text-[0.65rem] text-fg-faint">soon</span>
                      )}
                      {entry.planned && (
                        <span className="ml-1.5 text-[0.65rem] text-fg-faint">planned</span>
                      )}
                    </button>
                  );
                })}
              </section>
            ))}
          </div>
        </Sheet>
      )}
    </>
  );
}
