import { fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import { mockHttp } from "../test/harness";

mockHttp(["getPreflight"]);

const { DashboardShell } = await import("../panels/DashboardShell");
const { TokenRequired } = await import("./TokenRequired");

function shell(child: ReactNode, phone: boolean) {
  return (
    <DashboardShell
      appMode="parked"
      onToggleMode={() => {}}
      theme="dark"
      onToggleTheme={() => {}}
      telemetryState="open"
      anomalyState="open"
      controlState="closed"
      controlReady={false}
      connectivity="live-pi"
      events={[]}
      status={null}
      channels={new Map<string, ChannelSnapshot>()}
      token=""
      onTokenChange={() => {}}
      phone={phone}
    >
      {child}
    </DashboardShell>
  );
}

describe("TokenRequired", () => {
  it("says what is locked and where the token comes from", () => {
    render(<TokenRequired purpose="view tire wear" />);
    expect(screen.getByText("Locked — needs the control token to view tire wear.")).toBeVisible();
    expect(screen.getByText("canary-token").tagName).toBe("CODE");
    expect(screen.getByText(/pair this browser instead/)).toBeVisible();
  });

  it("opens the header token field, focused, from the Unlock button", async () => {
    render(shell(<TokenRequired purpose="view tire wear" />, false));
    expect(screen.queryByPlaceholderText("CANARY_CONTROL_TOKEN")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Unlock" }));
    const field = await screen.findByPlaceholderText("CANARY_CONTROL_TOKEN");
    expect(field).toHaveFocus();
  });

  it("opens the phone menu and then the field from the Unlock button", async () => {
    render(shell(<TokenRequired purpose="view tire wear" />, true));
    expect(screen.queryByRole("dialog", { name: "Menu" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Unlock" }));
    expect(await screen.findByRole("dialog", { name: "Menu" })).toBeVisible();
    const field = await screen.findByPlaceholderText("CANARY_CONTROL_TOKEN");
    expect(field).toHaveFocus();
  });

  it("does nothing, harmlessly, outside a shell", () => {
    render(<TokenRequired purpose="view tire wear" />);
    fireEvent.click(screen.getByRole("button", { name: "Unlock" }));
    expect(screen.queryByPlaceholderText("CANARY_CONTROL_TOKEN")).toBeNull();
  });
});
