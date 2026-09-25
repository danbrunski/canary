import { createElement } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { HttpErrorStub, mockHttp } from "../test/harness";

const mocks = mockHttp(["getPreflight"]);

const { TokenGate } = await import("./TokenGate");

function open() {
  fireEvent.click(screen.getByRole("button", { name: "control token" }));
}

describe("TokenGate (Task 5.1)", () => {
  it("reads 'required' with no token before the popover is opened (no probe)", () => {
    render(createElement(TokenGate, { token: "", onTokenChange: vi.fn() }));
    expect(screen.getByRole("button", { name: "control token" })).toHaveTextContent("required");
    expect(mocks.getPreflight).not.toHaveBeenCalled();
  });

  it("reads 'not needed' once opened against a loopback server with no token", async () => {
    mocks.getPreflight.mockResolvedValue({ ok: true, checked_at: "now", checks: [] });
    render(createElement(TokenGate, { token: "", onTokenChange: vi.fn() }));
    open();
    expect(await screen.findByText(/not needed/)).toBeInTheDocument();
  });

  it("reads 'rejected' when the server 401s a stored token", async () => {
    const err = new HttpErrorStub("rejected");
    err.status = 401;
    mocks.getPreflight.mockRejectedValue(err);
    render(createElement(TokenGate, { token: "stale-token", onTokenChange: vi.fn() }));
    open();
    expect(await screen.findByRole("button", { name: "control token" })).toHaveTextContent(
      "rejected",
    );
  });

  it("reads 'unlocked' once a token is accepted", async () => {
    mocks.getPreflight.mockResolvedValue({ ok: true, checked_at: "now", checks: [] });
    render(createElement(TokenGate, { token: "good-token", onTokenChange: vi.fn() }));
    open();
    expect(await screen.findByText(/unlocked/)).toBeInTheDocument();
  });

  it("calls onTokenChange as the operator types", async () => {
    mocks.getPreflight.mockResolvedValue({ ok: true, checked_at: "now", checks: [] });
    const onTokenChange = vi.fn();
    render(createElement(TokenGate, { token: "", onTokenChange }));
    open();
    await screen.findByText(/not needed/);
    fireEvent.change(screen.getByPlaceholderText("CANARY_CONTROL_TOKEN"), {
      target: { value: "new-token" },
    });
    expect(onTokenChange).toHaveBeenCalledWith("new-token");
  });
});
