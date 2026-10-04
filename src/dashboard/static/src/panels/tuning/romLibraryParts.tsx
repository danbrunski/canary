import type { ReactNode } from "react";

import type { PreconditionState } from "../../lib/romLibrary";
import { badgeClass, cn } from "../../styles";
import type {
  RomChecksumVerdict,
  RomConfidence,
  RomLibraryEntry,
  RomRevisionMatch,
} from "../../types/romLibrary";

/** One chip: the shared badge shape, toned by meaning. */
export function Chip({
  tone,
  title,
  children,
}: {
  tone: "ok" | "warn" | "bad" | "faint" | "uprev";
  title?: string;
  children: ReactNode;
}) {
  return (
    <span
      title={title}
      className={cn(
        badgeClass,
        "whitespace-nowrap normal-case tracking-[0.03em]",
        tone === "ok" && "border-ok/45 text-ok",
        tone === "warn" && "border-warn/45 text-warn",
        tone === "bad" && "border-bad/55 text-bad",
        tone === "faint" && "text-fg-dim",
        tone === "uprev" && "border-series-5/50 text-series-5",
      )}
    >
      {children}
    </span>
  );
}

const CONFIDENCE: Record<RomConfidence, { tone: "ok" | "warn" | "faint"; text: string }> = {
  verified: { tone: "ok", text: "Verified" },
  inferred: { tone: "warn", text: "Inferred" },
  unknown: { tone: "faint", text: "Unknown" },
};

export const CONFIDENCE_MEANING: Record<RomConfidence, string> = {
  verified: "checked against an independent known answer",
  inferred: "strong evidence, not yet confirmed",
  unknown: "the bytes are visible, not what they mean",
};

export function ConfidenceChip({ confidence }: { confidence: RomConfidence }) {
  const item = CONFIDENCE[confidence] ?? { tone: "faint", text: String(confidence) };
  return (
    <Chip tone={item.tone} title={CONFIDENCE_MEANING[confidence]}>
      {item.text}
    </Chip>
  );
}

export function UpRevChip() {
  return (
    <Chip tone="uprev" title="Added by UpRev's patch, not Nissan's">
      UpRev
    </Chip>
  );
}

/** The four chips and what each means, as the artifact's legend. */
export function ConfidenceLegend() {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-fg-dim">
      {(["verified", "inferred", "unknown"] as const).map((confidence) => (
        <span key={confidence} className="inline-flex items-center gap-1.5">
          <ConfidenceChip confidence={confidence} />
          {CONFIDENCE_MEANING[confidence]}
        </span>
      ))}
      <span className="inline-flex items-center gap-1.5">
        <UpRevChip />
        added by UpRev&apos;s patch, not the factory calibration
      </span>
    </div>
  );
}

export function RevisionChip({ match }: { match: RomRevisionMatch }) {
  if (match.revision)
    return (
      <Chip tone="ok" title="Plain-image sha256 equals this flashed revision's image_sha256">
        {match.revision}
      </Chip>
    );
  if (match.reference)
    return (
      <Chip tone="faint" title="A known image that was never flashed as-is">
        reference: {match.reference}
      </Chip>
    );
  if (match.status === "unknown")
    return (
      <Chip tone="warn" title={match.reason ?? "The flash history could not be read"}>
        revision unknown
      </Chip>
    );
  return (
    <Chip tone="faint" title="No recorded revision or reference image has this sha256">
      {match.status || "unrecognised"}
    </Chip>
  );
}

function words(values: Record<string, string | number> | null): string {
  if (!values) return "not read";
  return Object.entries(values)
    .map(([name, value]) => `${name} ${value}`)
    .join(", ");
}

export function checksumWords(checksum: RomChecksumVerdict): string {
  return `stored ${words(checksum.stored)}; computed ${words(checksum.computed)}`;
}

export function ChecksumChip({ checksum }: { checksum: RomChecksumVerdict | null }) {
  if (!checksum)
    return (
      <Chip tone="faint" title="No map definition to compute the checksum from">
        checksum n/a
      </Chip>
    );
  return checksum.ok ? (
    <Chip tone="ok" title={checksumWords(checksum)}>
      checksum ok
    </Chip>
  ) : (
    <Chip tone="bad" title={checksumWords(checksum)}>
      checksum fails
    </Chip>
  );
}

/** An ECU read that failed its integrity checks is kept, never as good. */
export function UnverifiedChip({ entry }: { entry: RomLibraryEntry }) {
  const meta = entry.read_meta;
  if (!meta || (meta.verified !== false && !meta.unverified_reason)) return null;
  return (
    <Chip tone="warn" title={meta.unverified_reason ?? undefined}>
      unverified
    </Chip>
  );
}

export function PreconditionChip({
  label,
  value,
  state,
}: {
  label: string;
  value: string;
  state: PreconditionState;
}) {
  return (
    <span data-precondition={state}>
      <Chip tone={state === "ok" ? "ok" : state === "blocked" ? "bad" : "faint"}>
        {label}: {value}
      </Chip>
    </span>
  );
}
