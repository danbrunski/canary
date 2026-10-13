import { definitionTermClass, placeholderClass } from "../styles";
import type { LiveProposals, MeasurementProposal } from "../types/brain";
import { formatTimeOfDay } from "../util/time";

/** One proposal in words: what the brain would do now and why. */
export function proposalText(p: MeasurementProposal): string {
  const where = p.region ? ` in ${p.region}` : "";
  switch (p.kind) {
    case "open_capture":
      return `propose capture ${p.profile ?? "?"}${where}`;
    case "collecting":
      return `collecting${where} (${p.reason}${p.profile ? `: ${p.profile}` : ""})`;
    case "review":
      return `after the drive: ${p.reason}`;
    default:
      return `waiting: ${p.reason}${where}`;
  }
}

/** The proposals split as delivered: the brain's state now, and the review
 *  items for after the drive. Nothing here is a prompt to the driver. */
export function splitProposals(block: LiveProposals): {
  now: MeasurementProposal[];
  afterDrive: MeasurementProposal[];
} {
  return {
    now: block.proposals.filter((p) => p.delivery === "brain"),
    afterDrive: block.proposals.filter((p) => p.delivery === "after_drive"),
  };
}

function ProposalList({ items, empty }: { items: MeasurementProposal[]; empty: string }) {
  if (items.length === 0) return <p className={placeholderClass}>{empty}</p>;
  return (
    <ul className="m-0 flex list-none flex-col gap-1 p-0">
      {items.map((p) => (
        <li key={`${p.measurement}|${p.residual}`}>
          <div>{p.measurement}</div>
          <div className={p.kind === "open_capture" ? "text-accent" : "text-fg-dim"}>
            {proposalText(p)}
            {p.detail ? ` — ${p.detail}` : ""}
          </div>
        </li>
      ))}
    </ul>
  );
}

/** The live next-measurement proposals (E03.4): which capture would be worth
 *  opening now, why each other measurement waits, and what to review after
 *  the drive. The brain only proposes; the operator opens any capture. */
export function ProposalsView({ block }: { block: LiveProposals }) {
  if (block.result !== "proposing") {
    return (
      <p className={placeholderClass}>
        {block.result}
        {block.detail ? ` — ${block.detail}` : ""}
      </p>
    );
  }
  const { now, afterDrive } = splitProposals(block);
  const opened = block.opened ?? [];
  return (
    <div className="flex flex-col gap-3 text-[0.86rem]">
      <div className="text-fg-dim">
        investigation {block.investigation ?? "—"} · ranked {block.method ?? "—"} · vehicle{" "}
        {block.vehicle_state ?? "unknown"}
        {block.capture?.active
          ? ` · operator capture ${block.capture.profile} (never displaced)`
          : ""}
      </div>
      <section>
        <h3 className={definitionTermClass}>Now</h3>
        <ProposalList items={now} empty="nothing to propose" />
      </section>
      <section>
        <h3 className={definitionTermClass}>After the drive</h3>
        <ProposalList items={afterDrive} empty="nothing to review" />
      </section>
      {opened.length > 0 && (
        <section>
          <h3 className={definitionTermClass}>Captures proposed this run</h3>
          <ul className="m-0 list-none p-0">
            {opened.map((o) => (
              <li key={`${o.at}|${o.measurement}`}>
                <span className="font-num text-fg-dim">{formatTimeOfDay(o.at)}</span>{" "}
                {o.profile ?? "?"} in {o.region ?? "?"} — {o.measurement}
              </li>
            ))}
          </ul>
        </section>
      )}
      {block.method === "structural" && (block.missing_likelihoods ?? []).length > 0 && (
        <p className="m-0 text-[0.8rem] text-fg-faint">
          ranked structurally; missing likelihoods: {(block.missing_likelihoods ?? []).join("; ")}
        </p>
      )}
    </div>
  );
}
