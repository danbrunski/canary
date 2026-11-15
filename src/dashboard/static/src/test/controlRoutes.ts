import { act, fireEvent, render } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, vi } from "vitest";

import { controlJson } from "../api/core";
import { RomEditorPage } from "../panels/tuning/RomEditorPage";
import { SHA_TUNED } from "./romLibraryFixtures";

// The control client, routed by `METHOD path`. Seven test files each wrote the
// same `control.mockImplementation` keyed on `${method} ${path}` (R28.4); this
// is the one copy.
//
// The calling test file still owns the module mock, because `vi.mock` is
// hoisted per file and cannot live in an imported helper (see `mockHttp` in
// `harness.ts` for the same constraint):
//
//   vi.mock("../../api/core", async (importOriginal) => ({
//     ...(await importOriginal<typeof import("../../api/core")>()),
//     controlJson: vi.fn(),
//   }));

/** The request a route is answering: its `init`, and its `METHOD path` key. */
export type RouteRequest = { init: RequestInit | undefined; key: string };

/** What one route answers. A value resolves; an `Error` rejects (a refusal, an
 *  unreachable daemon: `new HttpError(401, "")`, `new Error("pi went away")`);
 *  a function is called with the request and answers by the same rules, and one
 *  that throws rejects. A function is how a route reads the request body, counts
 *  its own calls, or changes its answer over time (`sequence`). */
export type Answer = AnswerFn | object | string | number | boolean | null | undefined;
type AnswerFn = (request: RouteRequest) => unknown;

/** `METHOD path` (query string included, as the page sends it) to its answer.
 *  A GET is written `GET /control/…`: a request with no method is a GET. */
export type RouteTable = Record<string, Answer>;

/** Answers for every key not in the table. Without it an unlisted request
 *  rejects `unexpected METHOD path`, so a page that starts asking for something
 *  new fails the test that did not expect it rather than reading `undefined`. */
export type RouteOptions = { fallback?: Answer };

/** The mocked `controlJson` of the calling file. */
function mockedControl() {
  if (!vi.isMockFunction(controlJson)) {
    throw new Error(
      "routeControl needs the test file to vi.mock the api/core module with controlJson: vi.fn()",
    );
  }
  return vi.mocked(controlJson);
}

function settle(answer: Answer, request: RouteRequest): Promise<never> {
  try {
    const value = typeof answer === "function" ? (answer as AnswerFn)(request) : answer;
    return value instanceof Error ? Promise.reject(value) : Promise.resolve(value as never);
  } catch (err) {
    return Promise.reject(err instanceof Error ? err : new Error(String(err)));
  }
}

/** Route the mocked control client by `METHOD path`. Call it from the test (or
 *  a `beforeEach`), once: each call replaces the previous table. */
export function routeControl(table: RouteTable, options: RouteOptions = {}) {
  const control = mockedControl();
  control.mockImplementation((path: string, _token: string, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${path}`;
    const request: RouteRequest = { init, key };
    if (key in table) return settle(table[key], request);
    if ("fallback" in options) return settle(options.fallback, request);
    return Promise.reject(new Error(`unexpected ${key}`));
  });
  return control;
}

/** An answer that changes with each request: the first call answers the first
 *  entry, the next the second, and the last entry answers every call after.
 *  `sequence(first, new Error("pi went away"))` is a read that works once and
 *  then fails: the follow-up read of an action (R28.3). */
export function sequence(...answers: Answer[]): Answer {
  if (answers.length === 0) throw new Error("sequence needs at least one answer");
  let next = 0;
  return (request: RouteRequest) => {
    const answer = answers[Math.min(next, answers.length - 1)];
    next += 1;
    return typeof answer === "function" ? (answer as AnswerFn)(request) : answer;
  };
}

/** The body a request was sent with, as the string it travelled as. */
export const bodyOf = (init?: RequestInit): string =>
  typeof init?.body === "string" ? init.body : "";

/** The JSON a request was sent with. */
export const jsonOf = <T = unknown>(init?: RequestInit): T => JSON.parse(bodyOf(init)) as T;

/** One recorded request to the control client. */
export type ControlCall = {
  path: string;
  method: string;
  token: string;
  init: RequestInit | undefined;
  /** The request body as sent ("" for none). */
  body: string;
};

/** How a request path is chosen: exact text, a pattern (`/\/simulate$/`), or a
 *  predicate. */
export type PathMatch = string | RegExp | ((path: string) => boolean);

const matches = (want: PathMatch, path: string): boolean =>
  typeof want === "string" ? path === want : want instanceof RegExp ? want.test(path) : want(path);

/** The requests made so far to `path` (and, when given, with `method`), in
 *  order. `callsTo(/\/download$/)` is how a test says a route was never asked. */
export function callsTo(path: PathMatch, method?: string): ControlCall[] {
  return mockedControl()
    .mock.calls.map(([callPath, token, init]): ControlCall => ({
      path: String(callPath),
      method: init?.method ?? "GET",
      token: String(token),
      init,
      body: bodyOf(init),
    }))
    .filter((call) => matches(path, call.path) && (method === undefined || call.method === method));
}

/** The JSON body of the first request to `path` (with `method`, when given), or
 *  `undefined` when there was none, so an assertion on it fails rather than
 *  throws when the request was never made. */
export function sentTo<T = unknown>(path: PathMatch, method?: string): T | undefined {
  const [call] = callsTo(path, method);
  return call ? (JSON.parse(call.body) as T) : undefined;
}

/** Every request made so far, in order. */
export const allCalls = (): ControlCall[] => callsTo(() => true);

/** A click, flushed: the effects it starts have run when this resolves. */
export async function click(element: HTMLElement): Promise<void> {
  await act(async () => {
    fireEvent.click(element);
  });
}

/** Set a field's value. */
export function type(element: HTMLElement, text: string): void {
  fireEvent.change(element, { target: { value: text } });
}

/** Choose a `<select>` option by value. */
export function select(element: HTMLElement, value: string): void {
  fireEvent.change(element, { target: { value } });
}

/** The state a test leaves in the browser: mock call records, fake timers, the
 *  URL (`?rom=<sha>` is how an editor test loads a base) and `localStorage` (the
 *  remembered simulation settings). Register it once per file, as `mockHttp`
 *  does for its own. */
export function resetControlRoutesAfterEach(): void {
  afterEach(() => {
    vi.clearAllMocks();
    vi.useRealTimers();
    window.history.replaceState(null, "", "/");
    window.localStorage.clear();
  });
}

/** The ROM Editor, as a reload or the desk launcher opens it: the editor starts
 *  unloaded, and a `?rom=<sha>` link loads a base at start. The link is set for
 *  every call, so a second visit after `cleanup()` starts from the same place
 *  (the page rewrites the URL as it is used). `null` leaves the URL as the test
 *  set it, for a test that opens its own link (`?rom=…&view=simulate`) or none. */
export function renderRomEditor(sha: string | null = SHA_TUNED) {
  if (sha) window.history.replaceState(null, "", `/?rom=${sha}`);
  return render(createElement(RomEditorPage, { token: "t", status: null }));
}
