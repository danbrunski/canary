import { useState } from "react";

import {
  SIM_SLOT,
  groupTables,
  isEditableTable,
  pendingEditCount,
  tableChoice,
  treeLabel,
  type TreeGroupId,
} from "../../../lib/romEditor";
import { cn, inputClass } from "../../../styles";
import type { PendingEdit } from "../../../types/romEditor";
import type { RomTableValues } from "../../../types/romLibrary";

function itemClass(chosen: boolean): string {
  return cn(
    "flex w-full min-w-0 items-center gap-1.5 rounded-[6px] border border-transparent px-2 py-1",
    "text-left text-[0.8rem] text-fg-dim transition-colors hover:text-fg",
    chosen && "border-border-soft bg-elev-2 text-fg",
  );
}

/** What a tree entry is, beyond its label: the full label (with its map
 *  slot), whether it takes an edit, its pending edits and an unverified
 *  decode. The tree shows these as marks; this is their text. */
function describe(table: RomTableValues, pending: number): string {
  return [
    table.label ?? table.name,
    isEditableTable(table) && "editable",
    pending > 0 && `${pending} pending ${pending === 1 ? "edit" : "edits"}`,
    table.confidence !== "verified" && `decode ${table.confidence}`,
  ]
    .filter(Boolean)
    .join(" · ");
}

/** The ROM Editor's table picker (W10.10): one slot's tables plus every
 *  single-copy table, grouped as `rom_map.yaml` files them, with a filter.
 *  Below 900px the same choice is a native select, one optgroup per group. */
export function TableTree({
  tables,
  chosen,
  slots,
  slot,
  pending,
  narrow,
  onSlot,
  onChoose,
}: {
  /** The tables listed for the current slot, in the daemon's order. */
  tables: readonly RomTableValues[];
  chosen: RomTableValues;
  slots: readonly number[];
  slot: number | null;
  pending: readonly PendingEdit[];
  narrow: boolean;
  onSlot: (slot: number) => void;
  onChoose: (table: RomTableValues) => void;
}) {
  const [filter, setFilter] = useState("");
  const [collapsed, setCollapsed] = useState<ReadonlySet<TreeGroupId>>(new Set());
  const filtering = filter.trim() !== "";

  const slotPicker = slots.length > 1 && (
    <label className="flex items-center gap-2 text-sm">
      Map slot
      <select
        aria-label="Map slot"
        className={cn(inputClass, "min-w-0 flex-1")}
        value={slot ?? ""}
        onChange={(event) => onSlot(Number(event.target.value))}
      >
        {slots.map((number) => (
          <option key={number} value={number}>
            Map {number}
            {number === SIM_SLOT ? " (editable)" : ""}
          </option>
        ))}
      </select>
    </label>
  );

  if (narrow) {
    const groups = groupTables(tables);
    return (
      <div className="flex flex-col gap-2">
        {slotPicker}
        <label className="flex items-center gap-2 text-sm">
          Table
          <select
            aria-label="Table"
            className={cn(inputClass, "min-w-0 flex-1")}
            value={tableChoice(chosen)}
            onChange={(event) => {
              const next = tables.find((table) => tableChoice(table) === event.target.value);
              if (next) onChoose(next);
            }}
          >
            {groups.map((group) => (
              <optgroup key={group.id} label={group.label}>
                {group.tables.map((table) => {
                  const count = pendingEditCount(pending, table);
                  return (
                    <option key={tableChoice(table)} value={tableChoice(table)}>
                      {treeLabel(table)}
                      {isEditableTable(table) ? " ✎" : ""}
                      {count > 0 ? ` (${count} pending)` : ""}
                    </option>
                  );
                })}
              </optgroup>
            ))}
          </select>
        </label>
      </div>
    );
  }

  const groups = groupTables(tables, filter);
  return (
    <nav aria-label="Tables" className="flex min-w-0 flex-col gap-3">
      {slotPicker}
      <input
        type="search"
        aria-label="Filter tables"
        placeholder="Filter tables"
        className={inputClass}
        value={filter}
        onChange={(event) => setFilter(event.target.value)}
      />
      {groups.length === 0 && (
        <p role="status" className="text-xs text-fg-dim">
          No table matches “{filter.trim()}”.
        </p>
      )}
      {groups.map((group) => {
        // A filter shows every match, collapsed group or not.
        const open = filtering || !collapsed.has(group.id);
        return (
          <div key={group.id} className="flex flex-col gap-0.5">
            <button
              type="button"
              aria-expanded={open}
              aria-label={`${group.label}, ${group.tables.length} ${
                group.tables.length === 1 ? "table" : "tables"
              }`}
              className="flex items-center gap-1.5 px-1 text-[0.62rem] font-bold uppercase tracking-[0.14em] text-fg-faint hover:text-fg"
              onClick={() =>
                setCollapsed((current) => {
                  const next = new Set(current);
                  if (next.has(group.id)) next.delete(group.id);
                  else next.add(group.id);
                  return next;
                })
              }
            >
              <span aria-hidden="true">{open ? "▾" : "▸"}</span>
              {group.label}
              <span className="ml-auto font-num tracking-normal">{group.tables.length}</span>
            </button>
            {open && (
              <ul className="flex flex-col gap-0.5">
                {group.tables.map((table) => {
                  const count = pendingEditCount(pending, table);
                  const isChosen = tableChoice(table) === tableChoice(chosen);
                  return (
                    <li key={tableChoice(table)}>
                      <button
                        type="button"
                        className={itemClass(isChosen)}
                        aria-current={isChosen ? "true" : undefined}
                        title={describe(table, count)}
                        onClick={() => onChoose(table)}
                      >
                        <span className="min-w-0 flex-1">{treeLabel(table)}</span>
                        {isEditableTable(table) && (
                          <span aria-hidden="true" className="text-accent-2">
                            ✎
                          </span>
                        )}
                        {count > 0 && (
                          <span
                            aria-hidden="true"
                            className="rounded-full bg-accent/20 px-1.5 font-num text-[0.65rem] text-accent"
                          >
                            {count}
                          </span>
                        )}
                        {table.confidence !== "verified" && (
                          <span
                            aria-hidden="true"
                            className="h-1.5 w-1.5 rounded-full bg-warn/70"
                          />
                        )}
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        );
      })}
    </nav>
  );
}
