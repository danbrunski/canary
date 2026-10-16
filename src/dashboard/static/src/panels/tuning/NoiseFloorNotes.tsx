import type { NoiseFloor, Significance } from "../../types/workbench";

/** The measured noise floor, in its own unit and with the conditions it was
 *  measured under; a floor that was never measured is said so. Shared by the
 *  delivery result and the revision comparison, which both state it the same
 *  way. */
export function FloorNote({ floor }: { floor: NoiseFloor | null | undefined }) {
  if (!floor) return null;
  if (!floor.available) {
    return <p className="m-0 text-xs text-fg-faint">Noise floor: not measured for this vehicle.</p>;
  }
  return (
    <p className="m-0 text-xs text-fg-dim">
      Noise floor (null run): sd {floor.sd?.toFixed(2)}{" "}
      {floor.unit === "percent" ? "%" : floor.unit}
      {floor.min != null && floor.max != null
        ? `, per-cell range ${floor.min.toFixed(2)} … ${floor.max.toFixed(2)}`
        : ", per-cell range not recorded"}
      {` over ${floor.n_cells} cells. Measured under: ${floor.conditions}.`} A result inside the
      band is not distinguishable from changing nothing.
    </p>
  );
}

/** The floor verdict for a comparison: a refused floor is named as refused,
 *  with both regimes, and never drawn as a band the deltas could sit inside. */
export function SignificanceNote({
  floor,
  significance,
}: {
  floor: NoiseFloor | null | undefined;
  significance: Significance | null | undefined;
}) {
  if (significance && !significance.available) {
    return (
      <p className="m-0 text-xs text-fg-dim" role="note" aria-label="Noise floor refused">
        Noise floor refused for {significance.channel}:{" "}
        {significance.floor_conditions
          ? `measured under "${significance.floor_conditions}", but this comparison is "${significance.requested_conditions}".`
          : significance.reason}{" "}
        No cell here has a significance verdict.
      </p>
    );
  }
  return <FloorNote floor={floor} />;
}
