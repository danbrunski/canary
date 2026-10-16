import { TableScroll } from "../../../components/TableScroll";
import { simulateReason } from "../../../lib/romEditor";
import { entryTitle } from "../../../lib/romLibrary";
import { buttonClass, cn } from "../../../styles";
import type { RomLibraryEntry } from "../../../types/romLibrary";
import { Chip, RevisionChip } from "../romLibraryParts";

/** Whether the simulator can use an image: a recorded revision only. */
export function SimulateChip({ reason }: { reason: string | null }) {
  return reason === null ? (
    <Chip tone="ok" title="A recorded revision: the simulator can use it as a base">
      can simulate
    </Chip>
  ) : (
    <Chip tone="warn" title={reason}>
      preview only
    </Chip>
  );
}

/** The library images an edit can start from: label, revision match, and
 *  whether the simulator can use the image (a matched revision only). */
export function BasePicker({
  entries,
  selectedSha,
  onChoose,
}: {
  entries: RomLibraryEntry[];
  selectedSha: string | null;
  onChoose: (sha: string) => void;
}) {
  return (
    <TableScroll>
      <table className="w-full min-w-[40rem] text-left text-sm" aria-label="Base images">
        <thead className="text-[0.7rem] uppercase tracking-[0.08em] text-fg-faint">
          <tr>
            <th className="py-2 pr-3">Image</th>
            <th className="py-2 pr-3">Calibration</th>
            <th className="py-2 pr-3">Revision</th>
            <th className="py-2 pr-3">Simulate</th>
            <th className="py-2" />
          </tr>
        </thead>
        <tbody>
          {entries.map((entry) => {
            const title = entryTitle(entry);
            const reason = simulateReason(entry.revision_match);
            const selected = entry.sha256 === selectedSha;
            return (
              <tr
                key={entry.sha256}
                className={cn("border-t border-border align-top", selected && "bg-elev-2/60")}
              >
                <td className="py-2 pr-3">
                  {title}
                  <span className="block font-num text-xs text-fg-faint">
                    {entry.sha256.slice(0, 12)}
                  </span>
                </td>
                <td className="py-2 pr-3 font-num">{entry.calibration_id ?? "not readable"}</td>
                <td className="py-2 pr-3">
                  <RevisionChip match={entry.revision_match} />
                </td>
                <td className="py-2 pr-3">
                  <SimulateChip reason={reason} />
                </td>
                <td className="py-2">
                  <button
                    type="button"
                    className={buttonClass}
                    aria-pressed={selected}
                    aria-label={`Edit from ${title}`}
                    onClick={() => onChoose(entry.sha256)}
                  >
                    {selected ? "Selected" : "Use"}
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </TableScroll>
  );
}
