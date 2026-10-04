import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { DiagnosticFunctionsResponse } from "../types/diagnostics";

vi.mock("../api/http", () => ({
  getDiagnosticFunctions: vi.fn(),
  clearModuleCodes: vi.fn(),
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
  });
});
