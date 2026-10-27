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
 *  Compensation (Map 1)"). The button is named by the shorter tree label; the
 *  full label leads its title. */
export function treeTable(label: string): HTMLElement {
  const tree = screen.getByRole("navigation", { name: "Tables" });
  const found = within(tree)
    .queryAllByRole("button")
    .find((item) => item.title.split(" · ")[0] === label);
  if (!found) throw new Error(`no table "${label}" in the tree`);
  return found;
}

/** `treeTable`, once the tables have loaded. */
export function findTreeTable(label: string): Promise<HTMLElement> {
  return waitFor(() => treeTable(label));
}

/** The tree top to bottom: each group's heading ("Fuel, 2 tables"), then its
 *  tables by full label. */
export function treeContents(): string[] {
  return within(screen.getByRole("navigation", { name: "Tables" }))
    .getAllByRole("button")
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
