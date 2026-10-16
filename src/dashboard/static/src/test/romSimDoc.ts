// The simulate route's request fields as `docs/headless-api.md` documents
// them, read from the doc itself so a field added to the route's contract and
// not to the page (or the other way round) fails a test rather than reaching
// the daemon as a 422.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/** The keys of the JSON example under "POST /control/rom/library/{sha}/simulate". */
export function documentedSimFields(): string[] {
  const text = readFileSync(resolve(process.cwd(), "../../../docs/headless-api.md"), "utf8");
  const at = text.indexOf("### `POST /control/rom/library/{sha}/simulate`");
  if (at < 0) throw new Error("docs/headless-api.md has no simulate section");
  const block = /```json\n([\s\S]*?)```/.exec(text.slice(at));
  if (!block?.[1]) throw new Error("the simulate section has no JSON request example");
  return [...block[1].matchAll(/"(\w+)":/g)].map((match) => match[1]);
}
