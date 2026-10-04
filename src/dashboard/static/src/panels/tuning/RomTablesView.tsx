import { useCallback, useState } from "react";

import { getRomTables } from "../../api/workbench";
import { useControlResource } from "../../hooks/useControlResource";
import {
  cellBytes,
  formatBytes,
  formatCell,
  hexAddress,
  isFuelTargets,
  mapTableOf,
  slotRegionName,
  tableSlots,
} from "../../lib/romLibrary";
import { mapColor } from "../../lib/romMaps";
import { inputClass } from "../../styles";
import type { RomConfidence, RomMapSlots, RomRegion, RomTableValues } from "../../types/romLibrary";
import { MapTable } from "../MapTable";
import { ConfidenceChip, UpRevChip } from "./romLibraryParts";

const SEGMENT_TONE: Record<RomConfidence, string> = {
  verified: "bg-ok/70",
  inferred: "bg-warn/70",
  unknown: "bg-fg-faint/70",
};

/** Every table of the selected image, per slot, with its confidence and
 *  evidence (W08.6). Fuel Targets is drawn with the ROM Maps `MapTable`;
 *  a table whose decode is unconfirmed is shown raw. */
export function RomTablesView({
  token,
  sha,
  mapSlots,
  regions,
  noMapReason,
}: {
  token: string;
  sha: string;
  mapSlots: RomMapSlots | null;
  /** The description's regions; the spans of the shown slot are drawn with
   *  its tables. */
  regions: RomRegion[];
  /** Set when the image has no map definition: the tables cannot exist. */
  noMapReason: string | null;
}) {
  const tables = useControlResource(
    useCallback((signal: AbortSignal) => getRomTables(token, sha, signal), [token, sha]),
    { enabled: Boolean(token) && !noMapReason, intervalMs: null },
  );
  const [chosenSlot, setChosenSlot] = useState<number | null>(null);

  if (noMapReason) return <p className="text-fg-dim">{noMapReason}: no tables to show.</p>;
  if (tables.loading || tables.stale) return <p className="text-fg-dim">Reading tables…</p>;
  if (tables.error) return <p className="text-bad">{tables.error}</p>;
  const all = tables.data?.tables ?? [];
  if (!all.length)
    return (
      <p className="text-fg-dim">
        {tables.data?.reason ?? "The map definition declares no tables for this image."}
      </p>
    );
  const slots = tableSlots(all);
  const slot = chosenSlot !== null && slots.includes(chosenSlot) ? chosenSlot : (slots[0] ?? null);
  const shown = all.filter((table) => table.slot === null || table.slot === slot);
  const slotTables = shown.filter((table) => table.slot !== null);
  return (
    <div className="space-y-5">
      {slots.length > 0 && (
        <label className="flex items-center gap-2 text-sm">
          Map slot
          <select
            aria-label="Table map slot"
            className={inputClass}
            value={slot ?? ""}
            onChange={(event) => setChosenSlot(Number(event.target.value))}
          >
            {slots.map((number) => (
              <option key={number} value={number}>
                Map {number}
              </option>
            ))}
          </select>
        </label>
      )}
      {mapSlots && slot !== null && (
        <SlotLayout
          mapSlots={mapSlots}
          slot={slot}
          tables={slotTables}
          regions={regions.filter((region) => region.slot === slot)}
        />
      )}
      {shown.map((table) => (
        <TableSection key={`${table.slot ?? "single"}-${table.id ?? table.name}`} table={table} />
      ))}
    </div>
  );
}

function TableSection({ table }: { table: RomTableValues }) {
  const asMap = isFuelTargets(table) ? mapTableOf(table) : null;
  const place = table.offset != null ? hexAddress(table.offset) : null;
  const shape = table.rows && table.cols ? `${table.rows} × ${table.cols}` : null;
  return (
    <section
      aria-label={`${table.name}${table.slot !== null ? ` slot ${table.slot}` : ""}`}
      className="space-y-2"
    >
      <h3 className="flex flex-wrap items-center gap-2 text-base font-semibold">
        {table.name}
        {table.slot !== null && <span className="text-sm text-fg-dim">Map {table.slot}</span>}
        <ConfidenceChip confidence={table.confidence} />
      </h3>
      <p className="font-num text-xs text-fg-faint">
        {[
          place,
          shape,
          table.dtype,
          table.decoded ? (table.decode?.formula ?? null) : "raw: decode unconfirmed",
        ]
          .filter(Boolean)
          .join(" · ")}
      </p>
      <p className="text-xs text-fg-dim">{table.evidence}</p>
      {asMap ? (
        <MapTable
          table={asMap}
          position={null}
          ecm={null}
          coverage={null}
          dwell={null}
          outOfRange={false}
          large={false}
        />
      ) : (
        <ValuesGrid table={table} />
      )}
    </section>
  );
}

/** A table that is not Fuel Targets: decoded values where a decode is
 *  declared, raw otherwise; a scalar (K) as one value. */
function ValuesGrid({ table }: { table: RomTableValues }) {
  const values: (number | null)[][] = table.decoded ?? table.raw;
  const unit = table.decoded ? (table.unit ?? "") : "raw";
  if (values.length === 1 && values[0]?.length === 1) {
    return (
      <p className="font-num text-lg" aria-label={`${table.name} value`}>
        {formatCell(values[0][0])} <span className="text-sm text-fg-dim">{unit}</span>
        {table.decoded && (
          <span className="ml-3 text-sm text-fg-faint">raw {formatCell(table.raw[0][0])}</span>
        )}
      </p>
    );
  }
  const flat = values.flat().filter((value): value is number => value !== null);
  const min = Math.min(...flat);
  const max = Math.max(...flat);
  const rowAxis = table.axes.rows;
  const colAxis = table.axes.cols;
  const rowLabels = rowAxis ? (rowAxis.decoded ?? rowAxis.raw) : null;
  const colLabels = colAxis ? (colAxis.decoded ?? colAxis.raw) : null;
  return (
    <div className="space-y-1">
      <p className="text-xs text-fg-dim">
        {rowAxis
          ? `Rows: ${rowAxis.name}${rowAxis.unit ? ` (${rowAxis.unit})` : ""}`
          : "Rows: index"}
        {" · "}
        {colAxis
          ? `Columns: ${colAxis.name}${colAxis.unit ? ` (${colAxis.unit})` : ""}`
          : "Columns: index"}
        {" · "}Cells: {unit || "value"}
      </p>
      <div className="overflow-x-auto rounded-lg border border-border">
        <table
          className="w-full border-separate border-spacing-1 text-center font-num text-xs"
          aria-label={`${table.name} values`}
        >
          {colLabels && (
            <thead>
              <tr>
                <th scope="col" className="p-1" />
                {colLabels.map((label, c) => (
                  <th key={c} scope="col" className="p-1 text-fg-dim">
                    {formatCell(label)}
                  </th>
                ))}
              </tr>
            </thead>
          )}
          <tbody>
            {values.map((row, r) => (
              <tr key={r}>
                <th scope="row" className="p-1 text-fg-dim">
                  {rowLabels ? formatCell(rowLabels[r] ?? r) : r}
                </th>
                {row.map((value, c) => (
                  <td
                    key={c}
                    title={`raw ${table.raw[r]?.[c] ?? "?"}`}
                    className="min-w-12 rounded p-1.5 text-bg"
                    style={{
                      backgroundColor: value === null ? undefined : mapColor(value, min, max),
                    }}
                  >
                    {formatCell(value)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

interface SlotSpan {
  name: string;
  start: number;
  end: number;
  confidence: RomConfidence;
  addedByUprev: boolean;
  evidence: string;
  table: boolean;
}

/** `+0x09C–0x0BF`, offsets from the slot's base. */
function slotSpanText(start: number, end: number): string {
  return start === end
    ? `+${hexAddress(start, 3)}`
    : `+${hexAddress(start, 3)}–${hexAddress(end, 3)}`;
}

/** Inside one map slot, as the artifact's slot diagram and table: the slot
 *  spans the definition declares (tables whose bounds are not known yet) and
 *  this slot's tables, at offsets from the slot's start. */
function SlotLayout({
  mapSlots,
  slot,
  tables,
  regions,
}: {
  mapSlots: RomMapSlots;
  slot: number;
  tables: RomTableValues[];
  /** This slot's spans, at absolute addresses. */
  regions: RomRegion[];
}) {
  const base = mapSlots.base + (slot - 1) * mapSlots.stride;
  const spans: SlotSpan[] = [
    ...regions.map((region) => ({
      name: slotRegionName(region),
      start: region.start - base,
      end: region.end - base,
      confidence: region.confidence,
      addedByUprev: region.added_by_uprev,
      evidence: region.evidence,
      table: false,
    })),
    ...tables.flatMap((table) => {
      if (table.offset == null) return [];
      const last =
        table.end ??
        (table.rows && table.cols
          ? table.offset + table.rows * table.cols * cellBytes(table) - 1
          : null);
      if (last == null) return [];
      const start = table.offset - base;
      const end = last - base;
      if (start < 0 || end >= mapSlots.stride) return [];
      return [
        {
          name: table.name,
          start,
          end,
          confidence: table.confidence,
          addedByUprev: false,
          evidence: table.evidence,
          table: true,
        },
      ];
    }),
  ].sort((a, b) => a.start - b.start || b.end - a.end);
  if (!spans.length) return null;
  const pct = (value: number) => `${((value / mapSlots.stride) * 100).toFixed(2)}%`;
  return (
    <section aria-label={`Inside Map ${slot}`} className="space-y-2">
      <p className="text-sm text-fg-dim">
        Inside Map {slot}: {formatBytes(mapSlots.stride)} from {hexAddress(base)}, offsets from the
        slot&apos;s start.
        {mapSlots.evidence ? ` ${mapSlots.evidence}` : ""}
      </p>
      <div
        role="img"
        aria-label={`Map ${slot} layout: ${spans
          .map((span) => `${span.name} at ${slotSpanText(span.start, span.start)}`)
          .join(", ")}`}
        className="relative h-9 overflow-hidden rounded bg-[repeating-linear-gradient(135deg,transparent_0_3px,var(--color-border)_3px_4px)] shadow-[inset_0_0_0_1px_var(--color-border)]"
      >
        {spans.map((span) => (
          <div
            key={`${span.name}-${span.start}`}
            title={`${span.name}: ${slotSpanText(span.start, span.end)}`}
            className={`absolute inset-y-0 min-w-[3px] ${SEGMENT_TONE[span.confidence] ?? SEGMENT_TONE.unknown}`}
            style={{ left: pct(span.start), width: pct(span.end - span.start + 1) }}
          />
        ))}
      </div>
      <div className="relative h-4 font-num text-[0.66rem] text-fg-faint">
        <span className="absolute left-0">+0x000</span>
        <span className="absolute right-0">+{hexAddress(mapSlots.stride - 1, 3)}</span>
      </div>
      <div className="overflow-x-auto rounded-lg border border-border">
        <table
          className="w-full min-w-[40rem] text-left text-sm"
          aria-label={`Map ${slot} contents`}
        >
          <thead className="text-[0.7rem] uppercase tracking-[0.08em] text-fg-faint">
            <tr>
              <th className="px-3 py-2">Offset</th>
              <th className="px-3 py-2">Size</th>
              <th className="px-3 py-2">What&apos;s there</th>
              <th className="px-3 py-2">Confidence</th>
              <th className="px-3 py-2">How we know</th>
            </tr>
          </thead>
          <tbody>
            {spans.map((span) => (
              <tr key={`${span.name}-${span.start}`} className="border-t border-border align-top">
                <td className="whitespace-nowrap px-3 py-2 font-num text-accent-2">
                  {slotSpanText(span.start, span.end)}
                </td>
                <td className="whitespace-nowrap px-3 py-2 font-num text-fg-dim">
                  {formatBytes(span.end - span.start + 1)}
                </td>
                <td className="px-3 py-2">
                  {span.name}
                  {span.table && <span className="ml-1.5 text-xs text-fg-faint">table</span>}
                </td>
                <td className="px-3 py-2">
                  <span className="flex flex-wrap gap-1">
                    {span.addedByUprev && <UpRevChip />}
                    <ConfidenceChip confidence={span.confidence} />
                  </span>
                </td>
                <td className="px-3 py-2 text-xs text-fg-dim">{span.evidence}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
