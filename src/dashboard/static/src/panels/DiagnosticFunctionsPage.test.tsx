import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { DiagnosticFunctionsResponse, ReadCodesResult } from "../types/diagnostics";

vi.mock("../api/http", () => ({
  getDiagnosticFunctions: vi.fn(),
  clearModuleCodes: vi.fn(),
  readModuleCodes: vi.fn(),
}));

const api = await import("../api/http");
const { DiagnosticFunctionsPage } = await import("./DiagnosticFunctionsPage");

function functions(
  overrides: Partial<DiagnosticFunctionsResponse["diagnostic_functions"]> = {},
): DiagnosticFunctionsResponse {
  return {
    diagnostic_functions: {
      functions: [
        {
          kind: "read_codes",
          transport: "kline",
          module: "SRS",
          address: "0x58",
          request_hex: "a2 00, a2 01",
          requests: [
            { request_hex: "a2 00", status: "current", status_confidence: "inferred" },
            { request_hex: "a2 01", status: "past", status_confidence: "inferred" },
          ],
          evidence: "The scan tool's code read, session 2026-10-08T172943.",
        },
        {
          kind: "clear_codes",
          transport: "kline",
          module: "SRS",
          address: "0x58",
          request_hex: "14",
          evidence: "The scan tool's clear, session 2026-10-04T194342.",
        },
      ],
      kline_available: true,
      blocked_reason: null,
      ...overrides,
    },
  };
}

afterEach(() => vi.clearAllMocks());

describe("DiagnosticFunctionsPage", () => {
  it("clears the airbag codes only after a confirm", async () => {
    vi.mocked(api.getDiagnosticFunctions).mockResolvedValue(functions());
    vi.mocked(api.clearModuleCodes).mockResolvedValue({
      clear_codes: {
        kind: "clear_codes",
        module: "SRS",
        address: "0x58",
        ok: true,
        reply_hex: "54",
        detail: "positive response",
        started_at: 0,
        finished_at: 1.2,
        duration_s: 1.2,
      },
    });
    render(<DiagnosticFunctionsPage token="t" />);

    fireEvent.click(await screen.findByRole("button", { name: "Clear codes" }));
    expect(api.clearModuleCodes).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Confirm: clear Airbag (SRS) codes" }));

    await waitFor(() => expect(api.clearModuleCodes).toHaveBeenCalledWith("t", "SRS"));
    expect(await screen.findByText(/Cleared: SRS answered 54/)).toBeTruthy();
  });

  it("shows a module's refusal as not cleared", async () => {
    vi.mocked(api.getDiagnosticFunctions).mockResolvedValue(functions());
    vi.mocked(api.clearModuleCodes).mockResolvedValue({
      clear_codes: {
        kind: "clear_codes",
        module: "SRS",
        address: "0x58",
        ok: false,
        reply_hex: null,
        detail: "SRS refused: conditionsNotCorrect",
        started_at: 0,
        finished_at: 1,
        duration_s: 1,
      },
    });
    render(<DiagnosticFunctionsPage token="t" />);
    fireEvent.click(await screen.findByRole("button", { name: "Clear codes" }));
    fireEvent.click(screen.getByRole("button", { name: /Confirm/ }));
    expect(await screen.findByText(/Not cleared: SRS refused/)).toBeTruthy();
  });

  it("disables the clear while the K-line is another tool's", async () => {
    vi.mocked(api.getDiagnosticFunctions).mockResolvedValue(
      functions({ blocked_reason: "passive mode is armed" }),
    );
    render(<DiagnosticFunctionsPage token="t" />);
    expect(await screen.findByText(/K-line unavailable: passive mode is armed/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Clear codes" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("button", { name: "Read codes" }).hasAttribute("disabled")).toBe(true);
  });

  it("reads the airbag codes and shows each with its status", async () => {
    vi.mocked(api.getDiagnosticFunctions).mockResolvedValue(functions());
    vi.mocked(api.readModuleCodes).mockResolvedValue({
      read_codes: readResult({
        codes: [{ code: "B1023", word: "0x9023", status: "past", status_confidence: "inferred" }],
        detail: "read current, past: 1 code(s) stored",
      }),
    });
    render(<DiagnosticFunctionsPage token="t" />);

    fireEvent.click(await screen.findByRole("button", { name: "Read codes" }));
    await waitFor(() => expect(api.readModuleCodes).toHaveBeenCalledWith("t", "SRS"));
    expect(await screen.findByText("B1023")).toBeTruthy();
    expect(screen.getByText("past")).toBeTruthy();
    expect(screen.getByText(/status inferred/)).toBeTruthy();
    expect(screen.getByText("Raw exchange")).toBeTruthy();
    expect(screen.getByText(/a2 01 → e2 01 90 23/)).toBeTruthy();
    expect(api.clearModuleCodes).not.toHaveBeenCalled();
  });

  it("says no codes are stored on a read that found none", async () => {
    vi.mocked(api.getDiagnosticFunctions).mockResolvedValue(functions());
    vi.mocked(api.readModuleCodes).mockResolvedValue({
      read_codes: readResult({ codes: [], detail: "read current, past: no codes stored" }),
    });
    render(<DiagnosticFunctionsPage token="t" />);
    fireEvent.click(await screen.findByRole("button", { name: "Read codes" }));
    expect(await screen.findByText(/No codes stored/)).toBeTruthy();
  });

  it("names a failed read rather than reporting no codes", async () => {
    vi.mocked(api.getDiagnosticFunctions).mockResolvedValue(functions());
    vi.mocked(api.readModuleCodes).mockResolvedValue({
      read_codes: readResult({
        ok: false,
        codes: null,
        detail: "SRS sent a malformed code reply to a2 01",
      }),
    });
    render(<DiagnosticFunctionsPage token="t" />);
    fireEvent.click(await screen.findByRole("button", { name: "Read codes" }));
    expect(await screen.findByText(/Not read: SRS sent a malformed code reply/)).toBeTruthy();
    expect(screen.queryByText(/No codes stored/)).toBeNull();
  });

  it("offers only the functions a module declares", async () => {
    const all = functions();
    all.diagnostic_functions.functions = all.diagnostic_functions.functions.filter(
      (fn) => fn.kind === "clear_codes",
    );
    vi.mocked(api.getDiagnosticFunctions).mockResolvedValue(all);
    render(<DiagnosticFunctionsPage token="t" />);
    expect(await screen.findByRole("button", { name: "Clear codes" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Read codes" })).toBeNull();
  });
});

function readResult(overrides: Partial<ReadCodesResult>): ReadCodesResult {
  return {
    kind: "read_codes",
    module: "SRS",
    address: "0x58",
    ok: true,
    reply_hex: "e2 01 90 23",
    detail: "",
    started_at: 0,
    finished_at: 0.4,
    duration_s: 0.4,
    codes: [],
    raw: [
      { request_hex: "a2 00", reply_hex: "e2 00" },
      { request_hex: "a2 01", reply_hex: "e2 01 90 23" },
    ],
    ...overrides,
  };
}
