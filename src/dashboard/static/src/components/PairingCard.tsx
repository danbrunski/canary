import type { PairingRequestStatus } from "../api/http";
import { usePairing } from "../hooks/usePairing";
import { instruction, keySteps } from "../pairing";
import { buttonClass, cn, noticeClass, panelClass, panelTitleClass } from "../styles";

interface PairingCardProps {
  onPaired: (token: string) => void;
}

function KeySteps({ status }: { status: PairingRequestStatus | null }) {
  return (
    <ol className="m-0 flex list-none flex-col gap-1.5 p-0">
      {keySteps(status?.step).map((step) => (
        <li
          key={step.label}
          className={cn(
            "flex items-center gap-2 text-[0.95rem]",
            step.done ? "text-ok" : step.current ? "font-semibold text-fg" : "text-fg-faint",
          )}
        >
          <span aria-hidden className="w-5 text-center">
            {step.done ? "✓" : step.current ? "→" : "·"}
          </span>
          {step.label}
        </li>
      ))}
    </ol>
  );
}

/** Pair this device by turning the key ACC -> ON -> ACC. Shown while the
 *  device has no control token. */
export function PairingCard({ onPaired }: PairingCardProps) {
  const { view, start, cancel } = usePairing(onPaired);
  return (
    <section className={cn(panelClass, "mx-auto my-3 w-full max-w-md")} aria-live="polite">
      <h2 className={panelTitleClass}>Pair this device</h2>
      {view.phase === "idle" && (
        <>
          <p className="mt-2 text-[0.9rem] text-fg-dim">
            To control CANary from this device, pair it from the driver's seat: you will turn the
            key from ACC to ON and back to ACC.
          </p>
          <button type="button" className={cn(buttonClass, "mt-3")} onClick={() => void start()}>
            Pair this device
          </button>
        </>
      )}
      {view.phase === "waiting" && (
        <>
          <KeySteps status={view.status} />
          <p
            className={cn(
              noticeClass(view.status?.step === "unavailable" ? "error" : "busy"),
              "mt-3",
            )}
          >
            {view.status === null ? "Asking the Pi…" : instruction(view.status)}
          </p>
          <button type="button" className={cn(buttonClass, "mt-3")} onClick={cancel}>
            Cancel
          </button>
        </>
      )}
      {view.phase === "failed" && (
        <>
          <p className={cn(noticeClass("error"), "mt-2")}>{view.message}</p>
          <button type="button" className={cn(buttonClass, "mt-3")} onClick={() => void start()}>
            Try again
          </button>
        </>
      )}
    </section>
  );
}
