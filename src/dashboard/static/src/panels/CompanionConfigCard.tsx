import { useCallback, useState } from "react";

import { clearPi, discoverPis, getCompanionStatus, HttpError, selectPi } from "../api/http";
import { Notice } from "../components/Notice";
import { describeError } from "../hooks/useControlResource";
import type { CompanionSettings } from "../hooks/useCompanionSettings";
import { usePolling } from "../hooks/usePolling";
import { PI_SOURCE_LABEL, piDisplayName } from "../lib/piLink";
import { badgeClass, buttonClass, inputClass, type NoticeState } from "../styles";
import type { CompanionStatusResponse, DiscoveredPi } from "../types";

interface CompanionConfigCardProps {
  companion: CompanionSettings;
  /** The dashboard's control token: it authorizes the Pi-link actions, and
   *  is the companion token whenever the companion is this server. */
  token: string;
}

/** `/companion/status` is a 404 when companion mode is off, which is an
 *  answer, not a failure. */
type StatusResult = { kind: "off" } | { kind: "on"; status: CompanionStatusResponse };

async function loadStatus(signal: AbortSignal): Promise<StatusResult> {
  try {
    return { kind: "on", status: await getCompanionStatus(signal) };
  } catch (err) {
    if (err instanceof HttpError && err.status === 404) return { kind: "off" };
    throw err;
  }
}

function formatRanAt(ranAt: string): string {
  const parsed = new Date(ranAt);
  return Number.isNaN(parsed.getTime()) ? ranAt : parsed.toLocaleString();
}

/** The Pi-link card: which Pi a companion-mode daemon is linked to, finding
 *  one on the network, and (advanced) pointing at a remote companion API.
 *  Every state that is not a value reads as words, never as a colour. */
export function CompanionConfigCard({ companion, token }: CompanionConfigCardProps) {
  const { data, error, loading, refresh } = usePolling(loadStatus, { intervalMs: 5000 });
  const [busy, setBusy] = useState<"find" | "use" | "unlink" | null>(null);
  const [sweep, setSweep] = useState(false);
  const [found, setFound] = useState<DiscoveredPi[] | null>(null);
  const [notice, setNotice] = useState<NoticeState | null>(null);

  const act = useCallback(
    async (kind: "find" | "use" | "unlink", work: () => Promise<void>) => {
      setBusy(kind);
      setNotice(null);
      try {
        await work();
        refresh();
      } catch (err) {
        setNotice({ kind: "error", text: describeError(err) });
      } finally {
        setBusy(null);
      }
    },
    [refresh],
  );

  const status = data?.kind === "on" ? data.status : null;
  const searching = busy === "find" || status?.discovery.state === "running";
  const list = found ?? status?.discovery.found ?? [];

  const find = () =>
    act("find", async () => {
      const response = await discoverPis(token, sweep);
      setFound(response.found);
    });
  const use = (pi: DiscoveredPi) =>
    act("use", async () => {
      await selectPi(token, pi.base_url);
    });
  const unlink = () =>
    act("unlink", async () => {
      await clearPi(token);
    });

  let statusLine: string;
  if (data?.kind === "off") {
    statusLine = "Companion mode is off (companion.enabled in the config)";
  } else if (status) {
    statusLine = `Library: ${status.library_root} · ${status.sessions} sessions · ${status.models} models`;
  } else if (error !== null) {
    statusLine = `Companion status unavailable: ${describeError(error)}`;
  } else {
    statusLine = loading ? "Reading companion status…" : "Companion status unavailable";
  }

  const pi = status?.pi ?? null;
  const needsToken = !token;

  return (
    <div className="flex flex-col gap-3 rounded-[8px] border border-border bg-elev p-3">
      <p className="m-0 text-[0.8rem] text-fg-dim">{statusLine}</p>

      {status && (
        <>
          <div className="flex flex-col gap-1.5">
            <div className="text-[0.72rem] uppercase tracking-[0.08em] text-fg-faint">
              Linked Pi
            </div>
            {pi === null ? (
              <p className="m-0 text-[0.85rem] text-fg-dim">No Pi linked</p>
            ) : (
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[0.85rem]">
                <strong className="font-semibold">{piDisplayName(pi)}</strong>
                {pi.identity?.profile && <span>{pi.identity.profile}</span>}
                {pi.identity?.version && <span>{pi.identity.version}</span>}
                <span className="font-num text-fg-dim">{pi.base_url}</span>
                <span className={badgeClass}>{PI_SOURCE_LABEL[pi.source]}</span>
                <button
                  className={buttonClass}
                  type="button"
                  onClick={unlink}
                  disabled={needsToken || busy !== null}
                >
                  Unlink
                </button>
              </div>
            )}
          </div>

          <div className="flex flex-col gap-1.5">
            <div className="flex flex-wrap items-center gap-3">
              <button
                className={buttonClass}
                type="button"
                onClick={find}
                disabled={needsToken || searching}
              >
                Find Pi
              </button>
              <label className="flex items-center gap-2 text-[0.75rem] text-fg-dim">
                <input
                  type="checkbox"
                  checked={sweep}
                  onChange={(event) => setSweep(event.target.checked)}
                />
                scan the whole subnet (slower)
              </label>
              <span className="text-[0.8rem] text-fg-dim">
                {searching
                  ? "Searching the network…"
                  : status.discovery.ran_at
                    ? `Last search ${formatRanAt(status.discovery.ran_at)}: ${list.length} found`
                    : "No search yet"}
              </span>
            </div>
            {needsToken && (
              <p className="m-0 text-[0.75rem] text-fg-dim">
                Enter the control token (lock icon in the header) to find or link a Pi.
              </p>
            )}
            {status.discovery.error && (
              <p className="m-0 text-[0.8rem]">Search failed: {status.discovery.error}</p>
            )}
            {list.length > 0 && (
              <ul className="m-0 flex list-none flex-col gap-1.5 p-0">
                {list.map((entry) => (
                  <li
                    key={entry.base_url}
                    className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[0.85rem]"
                  >
                    <strong className="font-semibold">{entry.identity.hostname}</strong>
                    {entry.identity.profile && <span>{entry.identity.profile}</span>}
                    <span className="font-num text-fg-dim">{entry.base_url}</span>
                    <span className={badgeClass}>{entry.source}</span>
                    <button
                      className={buttonClass}
                      type="button"
                      onClick={() => use(entry)}
                      disabled={needsToken || busy !== null}
                    >
                      Use
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </>
      )}

      <Notice notice={notice} />

      <div className="grid grid-cols-[minmax(18rem,1fr)_minmax(12rem,0.45fr)] items-end gap-3 max-[1100px]:grid-cols-1">
        <label className="flex flex-col gap-1 text-[0.75rem] text-fg-dim">
          Companion API (blank: this server)
          <input
            className={inputClass}
            value={companion.baseUrl}
            onChange={(event) => companion.setBaseUrl(event.target.value)}
            placeholder="blank: this server"
          />
        </label>
        <label className="flex flex-col gap-1 text-[0.75rem] text-fg-dim">
          Local token (remote companion only)
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
    </div>
  );
}
