import { useRef, useState, type KeyboardEvent } from "react";

import {
  BLOCK_CLASS_TEXT,
  absoluteRegions,
  BLOCK_SIZE,
  BLOCKS_PER_ROW,
  blockClassText,
  eraseSectorSummary,
  formatBytes,
  gridRows,
  hexAddress,
  hexSpan,
  regionsOverlapping,
  sortRegions,
} from "../../lib/romLibrary";
import { cn } from "../../styles";
import type { RomBlock, RomBlockClass, RomEraseSector, RomRegion } from "../../types/romLibrary";
import { ConfidenceChip, ConfidenceLegend, UpRevChip } from "./romLibraryParts";

/** One hue at four strengths: the colour says how the bytes look
 *  statistically, and must not read as good/bad. */
const BLOCK_CLASS_STYLE: Record<RomBlockClass, string> = {
  dense: "bg-accent-2",
  data: "bg-accent-2/55",
  sparse: "bg-accent-2/20",
  erased:
    "bg-[repeating-linear-gradient(135deg,transparent_0_3px,var(--color-border)_3px_4px)] " +
    "shadow-[inset_0_0_0_1px_var(--color-border)]",
};

function blockStyle(cls: string): string {
  return cls in BLOCK_CLASS_STYLE ? BLOCK_CLASS_STYLE[cls as RomBlockClass] : "bg-fg-faint";
}

/** The image at a glance: a grid of 4 KiB blocks, 64 KiB per row, with a
 *  readout on hover, tap or focus (W08.6). Arrow keys move between blocks;
 *  only the current block is in the tab order. */
export function RomBlockGrid({ blocks, regions }: { blocks: RomBlock[]; regions: RomRegion[] }) {
  const [selected, setSelected] = useState<number | null>(null);
  const cells = useRef<(HTMLButtonElement | null)[]>([]);
  const rows = gridRows(blocks);
  const current = selected ?? 0;
  const block = selected !== null ? blocks[selected] : undefined;

  function move(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const step: Record<string, number> = {
      ArrowRight: 1,
      ArrowLeft: -1,
      ArrowDown: BLOCKS_PER_ROW,
      ArrowUp: -BLOCKS_PER_ROW,
    };
    let next: number | null = null;
    if (event.key in step) next = index + step[event.key];
    else if (event.key === "Home") next = index - (index % BLOCKS_PER_ROW);
    else if (event.key === "End")
      next = Math.min(blocks.length - 1, index - (index % BLOCKS_PER_ROW) + BLOCKS_PER_ROW - 1);
    if (next === null || next < 0 || next >= blocks.length) return;
    event.preventDefault();
    cells.current[next]?.focus();
  }

  const overlapping = block
    ? regionsOverlapping(regions, block.offset, block.offset + BLOCK_SIZE - 1)
    : [];
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-fg-dim">
        {(Object.keys(BLOCK_CLASS_TEXT) as RomBlockClass[]).map((cls) => (
          <span key={cls} className="inline-flex items-center gap-1.5">
            <i
              aria-hidden="true"
              className={cn("inline-block h-3.5 w-3.5 rounded-sm", blockStyle(cls))}
            />
            {BLOCK_CLASS_TEXT[cls]}
          </span>
        ))}
      </div>
      <div className="grid gap-4 rounded-lg border border-border p-3 min-[900px]:grid-cols-[auto_minmax(0,1fr)]">
        <div className="overflow-x-auto">
          <div
            role="group"
            aria-label={`${formatBytes(BLOCK_SIZE)} blocks of the flash image`}
            className="grid grid-cols-[4.5rem_repeat(16,1.15rem)] items-center gap-0.5"
          >
            {rows.map((row, r) => (
              <RowCells
                key={r}
                row={row}
                rowIndex={r}
                current={current}
                selected={selected}
                register={(index, element) => {
                  cells.current[index] = element;
                }}
                onSelect={setSelected}
                onKey={move}
              />
            ))}
          </div>
        </div>
        <div
          aria-hidden="true"
          className="hidden min-w-0 flex-col gap-0.5 text-xs min-[900px]:flex"
        >
          {rows.map((row, r) => (
            <div
              key={r}
              className="flex h-[1.15rem] items-center gap-2 overflow-hidden whitespace-nowrap"
            >
              <span className="font-num text-fg-faint">{hexAddress(row[0].offset)}</span>
              <span className="truncate text-fg-dim">{rowNote(row, regions)}</span>
            </div>
          ))}
        </div>
        <div
          role="status"
          aria-live="polite"
          aria-label="Block readout"
          className="min-h-[2.8em] border-t border-dashed border-border pt-2 font-num text-sm min-[900px]:col-span-2"
        >
          {block ? (
            <>
              <div>
                {hexSpan(block.offset, block.offset + BLOCK_SIZE - 1)}
                <span className="text-fg-faint"> · </span>
                {blockClassText(block.class)}
                <span className="text-fg-faint"> · </span>
                {block.ff_pct.toFixed(0)} % 0xFF
                <span className="text-fg-faint"> · </span>
                entropy {block.entropy.toFixed(1)} bits/byte
              </div>
              <div className="text-fg-dim">
                {overlapping.length
                  ? overlapping.map((region) => region.name).join(" · ")
                  : "No mapped region covers this block."}
              </div>
            </>
          ) : (
            <span className="text-fg-dim">
              Select a block to see its address range, how much of it is erased, and which regions
              it overlaps.
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

function RowCells({
  row,
  rowIndex,
  current,
  selected,
  register,
  onSelect,
  onKey,
}: {
  row: RomBlock[];
  rowIndex: number;
  current: number;
  selected: number | null;
  register: (index: number, element: HTMLButtonElement | null) => void;
  onSelect: (index: number) => void;
  onKey: (event: KeyboardEvent<HTMLButtonElement>, index: number) => void;
}) {
  return (
    <>
      <span className="pr-1.5 text-right font-num text-[0.68rem] text-fg-faint">
        {hexAddress(row[0].offset)}
      </span>
      {row.map((block, c) => {
        const index = rowIndex * BLOCKS_PER_ROW + c;
        return (
          <button
            key={block.offset}
            ref={(element) => register(index, element)}
            type="button"
            tabIndex={index === current ? 0 : -1}
            aria-label={`${hexAddress(block.offset)}, ${blockClassText(block.class)}`}
            aria-current={selected === index ? "true" : undefined}
            data-block-class={block.class}
            className={cn(
              "h-[1.15rem] w-[1.15rem] rounded-sm border-0 p-0",
              "focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent",
              selected === index && "outline-2 outline-offset-1 outline-accent",
              blockStyle(block.class),
            )}
            onMouseEnter={() => onSelect(index)}
            onFocus={() => onSelect(index)}
            onClick={() => onSelect(index)}
            onKeyDown={(event) => onKey(event, index)}
          />
        );
      })}
    </>
  );
}

/** The absolute regions that start inside a 64 KiB row, largest first. */
function rowNote(row: RomBlock[], allRegions: RomRegion[]): string {
  const regions = absoluteRegions(allRegions);
  const start = row[0].offset;
  const end = row[row.length - 1].offset + BLOCK_SIZE - 1;
  const starting = regions
    .filter((region) => region.start >= start && region.start <= end)
    .sort((a, b) => b.end - b.start - (a.end - a.start));
  if (!starting.length) {
    const inside = regionsOverlapping(regions, start, end)[0];
    return inside ? `${inside.name} (continued)` : "";
  }
  const names = starting.slice(0, 3).map((region) => region.name);
  return starting.length > 3 ? `${names.join(" · ")} · …` : names.join(" · ");
}

/** The region-by-region table with its confidence chips and evidence. */
export function RomRegionTable({
  regions,
  eraseSectors,
}: {
  regions: RomRegion[];
  eraseSectors?: RomEraseSector[];
}) {
  return (
    <div className="space-y-3">
      <ConfidenceLegend />
      {eraseSectors && eraseSectors.length > 0 && (
        <p className="text-sm text-fg-dim">
          Erase sectors: {eraseSectorSummary(eraseSectors)}. The flash erases a whole sector at a
          time, so changing one cell rewrites the sector holding it.
        </p>
      )}
      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full min-w-[46rem] text-left text-sm" aria-label="Regions">
          <thead className="text-[0.7rem] uppercase tracking-[0.08em] text-fg-faint">
            <tr>
              <th className="px-3 py-2">Address</th>
              <th className="px-3 py-2">Size</th>
              <th className="px-3 py-2">What&apos;s there</th>
              <th className="px-3 py-2">Confidence</th>
              <th className="px-3 py-2">How we know</th>
            </tr>
          </thead>
          <tbody>
            {sortRegions(regions).map((region) => (
              <tr
                key={`${region.start}-${region.end}-${region.name}`}
                className="border-t border-border align-top"
              >
                <td className="whitespace-nowrap px-3 py-2 font-num text-accent-2">
                  {hexSpan(region.start, region.end)}
                </td>
                <td className="whitespace-nowrap px-3 py-2 font-num text-fg-dim">
                  {formatBytes(region.end - region.start + 1)}
                </td>
                <td className="px-3 py-2">
                  {region.name}
                  <span className="ml-1.5 text-xs text-fg-faint">{region.kind}</span>
                </td>
                <td className="px-3 py-2">
                  <span className="flex flex-wrap gap-1">
                    {region.added_by_uprev && <UpRevChip />}
                    <ConfidenceChip confidence={region.confidence} />
                  </span>
                </td>
                <td className="px-3 py-2 text-xs text-fg-dim">{region.evidence}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
