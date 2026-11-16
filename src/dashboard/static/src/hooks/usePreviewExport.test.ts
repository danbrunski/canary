import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { exportRomEdits, previewRomEdits } from "../api/workbench";
import type { RomEdit, RomEditExportResponse, RomEditPreview } from "../types/romEditor";
import { usePreviewExport } from "./usePreviewExport";

vi.mock("../api/workbench", () => ({
  previewRomEdits: vi.fn(),
  exportRomEdits: vi.fn(),
  downloadRomImage: vi.fn(),
}));

const SHA = "b".repeat(64);
const EDITS: RomEdit[] = [{ table: "fuel_targets", scale: 1.02 }];
const PREVIEW = { sim_base: { reason: null } } as unknown as RomEditPreview;
const EXPORTED = { entry: { sha256: "e".repeat(64) } } as unknown as RomEditExportResponse;

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((done) => (resolve = done));
  return { promise, resolve };
}

beforeEach(() => vi.resetAllMocks());

describe("usePreviewExport (R28.5)", () => {
  it("previews with the label trimmed and the edits it was given", async () => {
    vi.mocked(previewRomEdits).mockResolvedValue(PREVIEW);
    const { result } = renderHook(() => usePreviewExport("tok", SHA));
    await act(() => result.current.runPreview("  richer  ", EDITS));
    expect(previewRomEdits).toHaveBeenCalledWith("tok", SHA, { label: "richer", edits: EDITS });
    expect(result.current.preview).toEqual({ kind: "ok", data: PREVIEW });
  });

  it("drops a preview that lands after the edits changed", async () => {
    const slow = deferred<RomEditPreview>();
    vi.mocked(previewRomEdits).mockReturnValue(slow.promise);
    const { result } = renderHook(() => usePreviewExport("tok", SHA));
    let running: Promise<void> = Promise.resolve();
    act(() => {
      running = result.current.runPreview("a", EDITS);
    });
    expect(result.current.preview.kind).toBe("busy");
    act(() => result.current.invalidate());
    expect(result.current.preview.kind).toBe("idle");
    await act(async () => {
      slow.resolve(PREVIEW);
      await running;
    });
    expect(result.current.preview.kind).toBe("idle");
  });

  it("answers the latest preview only when two are in flight", async () => {
    const first = deferred<RomEditPreview>();
    const second = deferred<RomEditPreview>();
    vi.mocked(previewRomEdits)
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    const { result } = renderHook(() => usePreviewExport("tok", SHA));
    let a: Promise<void> = Promise.resolve();
    let b: Promise<void> = Promise.resolve();
    act(() => {
      a = result.current.runPreview("a", EDITS);
    });
    act(() => {
      b = result.current.runPreview("b", EDITS);
    });
    const late = { sim_base: { reason: "late" } } as unknown as RomEditPreview;
    await act(async () => {
      second.resolve(PREVIEW);
      await b;
      first.resolve(late);
      await a;
    });
    expect(result.current.preview).toEqual({ kind: "ok", data: PREVIEW });
  });

  it("exports under the preview's generation and drops an export once edits change", async () => {
    vi.mocked(previewRomEdits).mockResolvedValue(PREVIEW);
    const slow = deferred<RomEditExportResponse>();
    vi.mocked(exportRomEdits).mockReturnValue(slow.promise);
    const { result } = renderHook(() => usePreviewExport("tok", SHA));
    await act(() => result.current.runPreview("a", EDITS));
    let running: Promise<void> = Promise.resolve();
    act(() => {
      running = result.current.runExport("a", EDITS);
    });
    expect(result.current.exported.kind).toBe("busy");
    act(() => result.current.invalidate());
    await act(async () => {
      slow.resolve(EXPORTED);
      await running;
    });
    expect(result.current.exported.kind).toBe("idle");
    expect(result.current.preview.kind).toBe("idle");
  });

  it("shows an export that lands while its preview is still current", async () => {
    vi.mocked(previewRomEdits).mockResolvedValue(PREVIEW);
    vi.mocked(exportRomEdits).mockResolvedValue(EXPORTED);
    const { result } = renderHook(() => usePreviewExport("tok", SHA));
    await act(() => result.current.runPreview("a", EDITS));
    await act(() => result.current.runExport("a", EDITS));
    expect(result.current.exported).toEqual({ kind: "ok", data: EXPORTED });
  });

  it("shows a failed preview as an error", async () => {
    vi.mocked(previewRomEdits).mockRejectedValue(new Error("pi went away"));
    const { result } = renderHook(() => usePreviewExport("tok", SHA));
    await act(() => result.current.runPreview("a", EDITS));
    expect(result.current.preview).toEqual({ kind: "error", text: "pi went away" });
  });
});
