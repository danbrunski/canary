import { useState } from "react";

import { SegmentedControl, type SegmentOption } from "../../../components/SegmentedControl";
import { useLocalStorageJson } from "../../../hooks/useLocalStorage";
import {
  TABLE_KINDS,
  familyKey,
  isEditableTable,
  pendingEditCount,
  tableChoice,
  treeGroups,
  treeLabel,
  type TableKinds,
  type TreeEntry,
  type TreeGroupId,
} from "../../../lib/romEditor";
import { cn, inputClass } from "../../../styles";
import type { PendingEdit } from "../../../types/romEditor";
import type { RomTableValues } from "../../../types/romLibrary";

/** The Editable / View only / All choice, kept per browser (W10.15). */
export const TABLE_KINDS_STORAGE_KEY = "canary.romEditor.tableKinds.v1";

const KIND_OPTIONS: ReadonlyArray<SegmentOption<TableKinds>> = [
  { id: "all", label: "All" },
  { id: "editable", label: "Editable" },
  { id: "view", label: "View only" },
];

const EMPTY_KIND: Record<Exclude<TableKinds, "all">, string> = {
  editable: "No editable table in this image.",
  view: "No view-only table in this image.",
};

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

/** The ✎, pending-count and unverified-decode marks, hidden from the name
 *  (the title says them). */
function Marks({
  editable,
  pending,
  unverified,
}: {
  editable: boolean;
  pending: number;
  unverified: boolean;
}) {
  return (
    <>
      {editable && (
        <span aria-hidden="true" className="text-accent-2">
          ✎
        </span>
      )}
      {pending > 0 && (
        <span
          aria-hidden="true"
          className="rounded-full bg-accent/20 px-1.5 font-num text-[0.65rem] text-accent"
        >
          {pending}
        </span>
      )}
      {unverified && <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-warn/70" />}
    </>
  );
}

/** One option's text in the narrow select: the label, the slot for a copy
 *  in a family, and the marks as words. */
function optionText(table: RomTableValues, pending: number, inFamily: boolean): string {
  return [
    treeLabel(table),
    inFamily ? ` · Map ${table.slot}` : "",
    isEditableTable(table) ? " ✎" : "",
    pending > 0 ? ` (${pending} pending)` : "",
  ].join("");
}

function entryTables(entry: TreeEntry): RomTableValues[] {
  return entry.kind === "table" ? [entry.table] : entry.tables;
}

function readKinds(stored: unknown): TableKinds {
  return TABLE_KINDS.find((kinds) => kinds === stored) ?? "all";
}

/** The ROM Editor's table picker (W10.10, W10.14-W10.15): every listed table
 *  of every slot, grouped as `rom_map.yaml` files them, a slotted table as one
 *  family over its Map 1-5 copies, with Editable / View only / All and a text
 *  filter. Below 900px the same choice is a native select, one optgroup per
 *  group. */
export function TableTree({
  tables,
  chosen,
  pending,
  narrow,
  onChoose,
}: {
  /** Every listed table, all slots, in the daemon's order. */
  tables: readonly RomTableValues[];
  chosen: RomTableValues;
  pending: readonly PendingEdit[];
  narrow: boolean;
  onChoose: (table: RomTableValues) => void;
}) {
  const [filter, setFilter] = useState("");
  const [storedKinds, setKinds] = useLocalStorageJson<unknown>(TABLE_KINDS_STORAGE_KEY, "all");
  const kinds = readKinds(storedKinds);
  const [collapsed, setCollapsed] = useState<ReadonlySet<TreeGroupId>>(new Set());
  // Families start closed but for the chosen table's own, which also opens
  // when the choice arrives from elsewhere (the narrow select, then a wider
  // window).
  const chosenFamily = familyKey(chosen);
  const [openFamilies, setOpenFamilies] = useState<ReadonlySet<string>>(
    () => new Set(chosenFamily === null ? [] : [chosenFamily]),
  );
  const [openedFor, setOpenedFor] = useState(chosenFamily);
  if (chosenFamily !== openedFor) {
    setOpenedFor(chosenFamily);
    if (chosenFamily !== null) setOpenFamilies((current) => new Set(current).add(chosenFamily));
  }
  const filtering = !narrow && filter.trim() !== "";
  const groups = treeGroups(tables, filtering ? filter : "", kinds);

  const kindPicker = (
    <SegmentedControl
      label="Show tables"
      options={KIND_OPTIONS}
      value={kinds}
      onChange={setKinds}
      className="flex-wrap"
      compact
    />
  );
  const empty = groups.length === 0 && (
    <p role="status" className="text-xs text-fg-dim">
      {filtering
        ? `No table matches “${filter.trim()}”.`
        : kinds === "all"
          ? "This image lists no table."
          : EMPTY_KIND[kinds]}
    </p>
  );

  if (narrow) {
    const listed = groups.flatMap((group) => group.entries.flatMap(entryTables));
    return (
      <div className="flex flex-col gap-2">
        {kindPicker}
        {empty}
        {groups.length > 0 && (
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
              {/* The chosen table stays the select's value when a kind hides it. */}
              {!listed.includes(chosen) && (
                <option value={tableChoice(chosen)}>
                  {optionText(
                    chosen,
                    0,
                    tables.some((table) => table !== chosen && familyKey(table) === chosenFamily),
                  )}
                </option>
              )}
              {groups.map((group) => (
                <optgroup key={group.id} label={group.label}>
                  {group.entries.flatMap((entry) =>
                    entryTables(entry).map((table) => (
                      <option key={tableChoice(table)} value={tableChoice(table)}>
                        {optionText(
                          table,
                          pendingEditCount(pending, table),
                          entry.kind === "family",
                        )}
                      </option>
                    )),
                  )}
                </optgroup>
              ))}
            </select>
          </label>
        )}
      </div>
    );
  }

  const leaf = (table: RomTableValues, name: string) => {
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
          <span className="min-w-0 flex-1">{name}</span>
          <Marks
            editable={isEditableTable(table)}
            pending={count}
            unverified={table.confidence !== "verified"}
          />
        </button>
      </li>
    );
  };

  const family = (entry: Extract<TreeEntry, { kind: "family" }>) => {
    const open = filtering || openFamilies.has(entry.key);
    const count = entry.tables.reduce((sum, table) => sum + pendingEditCount(pending, table), 0);
    const maps = `${entry.tables.length} maps`;
    return (
      <li key={`family:${entry.key}`} className="flex flex-col gap-0.5">
        <button
          type="button"
          data-tree="family"
          className={itemClass(false)}
          aria-expanded={open}
          aria-label={`${entry.label}, ${maps}`}
          title={`${entry.label} · ${maps}`}
          onClick={() =>
            setOpenFamilies((current) => {
              const next = new Set(current);
              if (next.has(entry.key)) next.delete(entry.key);
              else next.add(entry.key);
              return next;
            })
          }
        >
          <span aria-hidden="true" className="text-fg-faint">
            {open ? "▾" : "▸"}
          </span>
          <span className="min-w-0 flex-1">{entry.label}</span>
          <Marks
            editable={entry.tables.some(isEditableTable)}
            pending={count}
            unverified={entry.tables.some((table) => table.confidence !== "verified")}
          />
        </button>
        {open && (
          <ul
            role="group"
            aria-label={entry.label}
            className="ml-3 flex flex-col gap-0.5 border-l border-border-soft pl-1"
          >
            {entry.tables.map((table) => leaf(table, `Map ${table.slot}`))}
          </ul>
        )}
      </li>
    );
  };

  return (
    <nav aria-label="Tables" className="flex min-w-0 flex-col gap-3">
      {kindPicker}
      <input
        type="search"
        aria-label="Filter tables"
        placeholder="Filter tables"
        className={inputClass}
        value={filter}
        onChange={(event) => setFilter(event.target.value)}
      />
      {empty}
      {groups.map((group) => {
        // A filter shows every match, collapsed group or not.
        const open = filtering || !collapsed.has(group.id);
        return (
          <div key={group.id} className="flex flex-col gap-0.5">
            <button
              type="button"
              aria-expanded={open}
              aria-label={`${group.label}, ${group.entries.length} ${
                group.entries.length === 1 ? "table" : "tables"
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
              <span className="ml-auto font-num tracking-normal">{group.entries.length}</span>
            </button>
            {open && (
              <ul className="flex flex-col gap-0.5">
                {group.entries.map((entry) =>
                  entry.kind === "table"
                    ? leaf(entry.table, treeLabel(entry.table))
                    : family(entry),
                )}
              </ul>
            )}
          </div>
        );
      })}
    </nav>
  );
}
