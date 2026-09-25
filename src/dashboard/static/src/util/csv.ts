/** CSV of exactly the rows and columns given — a table's current view.
 *  Moved from lib/workbenchApi (R08.4). */
export function toCsv(
  header: string[],
  rows: Array<Array<string | number | boolean | null>>,
): string {
  const escape = (value: string | number | boolean | null) => {
    if (value === null) return "";
    const text = String(value);
    return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  return [header, ...rows].map((row) => row.map(escape).join(",")).join("\n") + "\n";
}
