import type { CompanionSettings } from "../hooks/useCompanionSettings";
import { inputClass } from "../styles";

interface CompanionConfigCardProps {
  companion: CompanionSettings;
}

/** The companion (laptop) API URL + local token fields, extracted from
 *  ControlPanel's god-panel (Task 3.2). */
export function CompanionConfigCard({ companion }: CompanionConfigCardProps) {
  return (
    <div className="grid grid-cols-[minmax(18rem,1fr)_minmax(12rem,0.45fr)] items-end gap-3 rounded-[8px] border border-border bg-elev p-3 max-[1100px]:grid-cols-1">
      <label className="flex flex-col gap-1 text-[0.75rem] text-fg-dim">
        Companion API
        <input
          className={inputClass}
          value={companion.baseUrl}
          onChange={(event) => companion.setBaseUrl(event.target.value)}
          placeholder="http://127.0.0.1:8765"
        />
      </label>
      <label className="flex flex-col gap-1 text-[0.75rem] text-fg-dim">
        Local token
        <input
          className={inputClass}
          type="password"
          value={companion.token}
          onChange={(event) => companion.setToken(event.target.value)}
          placeholder="optional"
          autoComplete="off"
        />
      </label>
    </div>
  );
}
