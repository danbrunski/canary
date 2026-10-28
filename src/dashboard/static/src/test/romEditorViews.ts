import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";

export type RomViewName = "Tables" | "Edits & Preview" | "Simulate";

/** Show one of the ROM Editor's views (W10.9). The others stay mounted but
 *  `hidden`, and a role query never finds what is hidden. */
export async function showRomView(view: RomViewName): Promise<void> {
  const views = await screen.findByRole("group", { name: "ROM Editor views" });
  const button = within(views)
    .getAllByRole("button")
    .find((item) => item.textContent?.startsWith(view));
  if (!button) throw new Error(`no ROM Editor view "${view}"`);
  await act(async () => {
    fireEvent.click(button);
  });
}

/** The table tree's button for a table, by its full label ("Fuel
 *  Compensation (Map 1)"). The button is named by the shorter tree label (a
 *  slotted copy by "Map 1"); the full label leads its title. A copy inside a
 *  closed family is found by opening each closed family in turn (W10.14). */
export function treeTable(label: string): HTMLElement {
  const tree = screen.getByRole("navigation", { name: "Tables" });
  const find = () =>
    within(tree)
      .queryAllByRole("button")
      .find((item) => item.dataset.tree !== "family" && item.title.split(" · ")[0] === label);
  let found = find();
  for (const family of tree.querySelectorAll<HTMLElement>(
    '[data-tree="family"][aria-expanded="false"]',
  )) {
    if (found) break;
    fireEvent.click(family);
    found = find();
    if (!found) fireEvent.click(family);
  }
  if (!found) throw new Error(`no table "${label}" in the tree`);
  return found;
}

/** `treeTable`, once the tables have loaded. */
export function findTreeTable(label: string): Promise<HTMLElement> {
  return waitFor(() => treeTable(label));
}

/** The tree top to bottom: each group's heading ("Fuel, 2 tables"), each
 *  family's row ("Fuel Targets, 5 maps") and, when it is open, its copies,
 *  and each table by full label. The Show tables choice is left out. */
export function treeContents(): string[] {
  const tree = screen.getByRole("navigation", { name: "Tables" });
  const kinds = within(tree).getByRole("group", { name: "Show tables" });
  return within(tree)
    .getAllByRole("button")
    .filter((button) => !kinds.contains(button))
    .map((button) => button.getAttribute("aria-label") ?? button.title.split(" · ")[0] ?? "");
}

/** A library image's button in the loaded page's Recent ▾ menu (W10.8),
 *  opening the menu first if it is closed. */
export function recentEntry(name: RegExp | string): HTMLElement {
  if (!screen.queryByRole("dialog", { name: "Recent ROMs" })) {
    fireEvent.click(screen.getByRole("button", { name: "Recent" }));
  }
  const menu = screen.getByRole("dialog", { name: "Recent ROMs" });
  return within(menu).getByRole("button", { name });
}

/** Load a library image from the Recent ▾ menu. */
export function chooseRecent(name: RegExp | string): void {
  fireEvent.click(recentEntry(name));
}
