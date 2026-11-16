/** What a lazily loaded page shows while its chunk arrives: the same quiet
 *  italic line the panels use for their own loads, announced to a screen
 *  reader as a status. */
export function PageLoading() {
  return (
    <p role="status" className="m-0 p-3 text-fg-faint italic">
      Loading…
    </p>
  );
}
