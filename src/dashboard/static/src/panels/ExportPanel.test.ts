import { createElement } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { mockHttp } from "../test/harness";
import type { JobRecord } from "../types";

const mocks = mockHttp(["submitJob", "listJobs", "getJobLogs"]);

const { ExportPanel } = await import("./ExportPanel");

function job(overrides: Partial<JobRecord> = {}): JobRecord {
  return {
    job_id: "job-1",
    kind: "export_dataset",
    status: "queued",
    created_at: "2026-07-19T00:00:00Z",
    started_at: null,
    finished_at: null,
    returncode: null,
    log_path: "/tmp/job-1.log",
    command: [],
    params: {},
    ...overrides,
  };
}

describe("ExportPanel", () => {
  it("requires a token before submitting", async () => {
    render(createElement(ExportPanel, { token: "" }));
    fireEvent.change(screen.getByPlaceholderText(/2026-05-11T222447/), {
      target: { value: "2026-05-11T222447" },
    });
    fireEvent.click(screen.getByRole("button", { name: "export" }));
    expect(await screen.findByText(/control token/)).toBeInTheDocument();
    expect(mocks.submitJob).not.toHaveBeenCalled();
  });

  it("submits an export_dataset job with the entered params", async () => {
    mocks.submitJob.mockResolvedValue({ job: job() });
    render(createElement(ExportPanel, { token: "secret" }));

    fireEvent.change(screen.getByPlaceholderText(/2026-05-11T222447/), {
      target: { value: "2026-05-11T222447,2026-05-13T004818" },
    });
    fireEvent.change(screen.getByPlaceholderText("lstm baseline"), {
      target: { value: "megalogviewer" },
    });
    fireEvent.click(screen.getByRole("button", { name: "export" }));

    await screen.findByText("job-1");
    expect(mocks.submitJob).toHaveBeenCalledWith(
      "secret",
      "export_dataset",
      expect.objectContaining({
        session_ids: ["2026-05-11T222447", "2026-05-13T004818"],
        formats: ["csv"],
        purpose: "megalogviewer",
        zip: true,
      }),
    );
  });

  it("shows job logs after refreshing a finished job", async () => {
    mocks.submitJob.mockResolvedValue({ job: job() });
    mocks.listJobs.mockResolvedValue({ jobs: [job({ status: "succeeded" })], count: 1 });
    mocks.getJobLogs.mockResolvedValue({ job_id: "job-1", log: "DATASET EXPORT" });
    render(createElement(ExportPanel, { token: "secret" }));

    fireEvent.change(screen.getByPlaceholderText(/2026-05-11T222447/), {
      target: { value: "2026-05-11T222447" },
    });
    fireEvent.click(screen.getByRole("button", { name: "export" }));
    await screen.findByText("job-1");

    fireEvent.click(screen.getByRole("button", { name: "refresh status" }));

    expect(await screen.findByText("DATASET EXPORT")).toBeInTheDocument();
  });
});
