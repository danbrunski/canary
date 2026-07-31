import type { CaptureProfileState } from "../types";

export function CaptureProfileBanner({ state }: { state?: CaptureProfileState | null }) {
  if (!state?.active) return null;
  return (
    <div
      className="border-b border-warn/50 bg-warn/15 px-4 py-2 text-center text-[0.78rem] font-semibold text-warn"
      role="status"
    >
      CAPTURE: {state.name} · {state.session_dir ?? "rotating storage"}
    </div>
  );
}
