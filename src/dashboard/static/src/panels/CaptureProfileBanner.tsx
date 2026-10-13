import type { CaptureProfileState } from "../types";

/** The running capture, on every page. Below 600px (P5.1) it is one
 *  truncated line: the profile and its rate stay visible, the session
 *  directory drops out (the full text is the tooltip and the accessible
 *  status). */
export function CaptureProfileBanner({ state }: { state?: CaptureProfileState | null }) {
  if (!state?.active) return null;
  const details = [
    state.experimental && "EXPERIMENTAL",
    state.target_rate_hz != null && `${state.target_rate_hz} Hz/channel`,
    state.did_continuous && "Mode 22 continuous",
  ].filter(Boolean);
  const where = state.session_dir ?? "rotating storage";
  return (
    <div
      className="border-b border-warn/50 bg-warn/15 px-4 py-2 text-center text-[0.78rem] font-semibold text-warn max-[600px]:truncate max-[600px]:px-3 max-[600px]:py-1 max-[600px]:text-left max-[600px]:text-[0.72rem]"
      role="status"
      title={[`CAPTURE: ${state.name}`, ...details, where].join(" · ")}
    >
      CAPTURE: {state.name}
      {details.map((detail) => ` · ${String(detail)}`).join("")}
      <span className="max-[600px]:hidden">
        {" · "}
        {where}
      </span>
    </div>
  );
}
