import type { Page } from "@playwright/test";

import { CAPTURE_ID, LOGS, delivery, longFrame } from "../../src/test/workbenchFixtures";
import type { DeliveryErrorResponse } from "../../src/types/workbench";
import { mockDaemon } from "./mockDaemon";

export { CAPTURE_ID };

export const RUNNING_REVISION = "ZS44A-20260920";

/** The Logs and Analysis routes with real shapes: a capture list, a long
 *  frame for the selected capture, and a delivery-error result. Call before
 *  `page.goto`. Browser-level stubs registered after `mockDaemon` take
 *  precedence over its catch-all, and `/analysis/*` is not one of its
 *  prefixes at all. */
export async function mockWorkbench(
  page: Page,
  options: { rows?: number; bands?: number } = {},
): Promise<void> {
  const rows = options.rows ?? 2000;
  await mockDaemon(page, { status: { registry_revision: RUNNING_REVISION } });
  await page.addInitScript((id) => {
    localStorage.setItem("canary.controlToken", "ui-test");
    localStorage.setItem("canary.tuning.selectedLogId.v1", JSON.stringify(id));
  }, CAPTURE_ID);
  await page.route(/\/control\/logs(\?|$)/, (route) => route.fulfill({ json: LOGS }));
  await page.route(/\/control\/sessions\/[^/]+\/frame(\?|$)/, (route) =>
    route.fulfill({ json: longFrame(rows) }),
  );
  const base = delivery();
  const bands = Array.from({ length: options.bands ?? 2 }, (_, i) => ({
    ...base.bands[0],
    rpm_band: `(${2500 + i * 500}.0, ${3000 + i * 500}.0]`,
    n: 8 + i,
    error_pct: i % 2 ? -1.5 : 2,
  }));
  const result: DeliveryErrorResponse = { ...base, bands };
  await page.route(/\/analysis\/delivery-error(\?|$)/, (route) => route.fulfill({ json: result }));
}
