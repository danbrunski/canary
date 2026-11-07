import { buttonClass, cn } from "../styles";
import { TokenHint } from "./TokenHint";
import { useTokenEntry } from "./TokenEntry";

interface TokenRequiredProps {
  /** What the token unlocks, as a verb phrase: "view tire wear". */
  purpose: string;
  className?: string;
}

/** The one locked-state placeholder for a panel that needs the control
 *  token: says what it is waiting for, opens the header token entry
 *  (`TokenGate`) on Unlock, and says where the token comes from. Use it
 *  instead of writing "enter the token" text, which has gone stale once
 *  already (entry moved from the Control panel to the header). */
export function TokenRequired({ purpose, className }: TokenRequiredProps) {
  const { requestTokenEntry } = useTokenEntry();
  return (
    <p
      className={cn(
        "m-0 flex flex-wrap items-center gap-x-2 gap-y-1 italic text-fg-faint",
        className,
      )}
    >
      <span>{`Locked — needs the control token to ${purpose}.`}</span>
      <button
        type="button"
        className={cn(buttonClass, "min-h-6 px-2.5 py-0.5 text-[0.75rem] not-italic")}
        onClick={requestTokenEntry}
      >
        Unlock
      </button>
      <span className="text-[0.75rem] not-italic">
        <TokenHint />
      </span>
    </p>
  );
}
