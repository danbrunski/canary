import { useCallback, useState } from "react";

import { getProfiles, updateProfileName } from "../api/http";
import { Notice } from "../components/Notice";
import { Panel } from "../components/Panel";
import { ResourceGate } from "../components/ResourceGate";
import { useAsyncAction } from "../hooks/useAsyncAction";
import { useControlResource } from "../hooks/useControlResource";
import { buttonClass, cn, inputClass, placeholderClass } from "../styles";

interface ProfilesPanelProps {
  token: string;
}

/** Vehicle profiles (multi-vehicle support). Each vehicle the Pi has been
 *  plugged into is identified by its VIN and registered automatically with a
 *  blank display name; the operator names it here ("2008 Xterra") and can edit
 *  later. The name is cosmetic — the stable `slug` keys the data tree, so a
 *  rename never moves anything. */
export function ProfilesPanel({ token }: ProfilesPanelProps) {
  const fetcher = useCallback(
    (signal: AbortSignal) => getProfiles(token, signal),
    [token],
  );
  const { data, error, loading, refresh } = useControlResource(fetcher, {
    enabled: Boolean(token),
    intervalMs: 30000,
  });

  const [editingSlug, setEditingSlug] = useState<string | null>(null);
  const [draftName, setDraftName] = useState("");
  const { notice, busy, run } = useAsyncAction();

  const startEdit = (slug: string, name: string) => {
    setEditingSlug(slug);
    setDraftName(name);
  };

  const onSave = (slug: string) =>
    run("save", async () => {
      await updateProfileName(token, slug, draftName.trim());
      setEditingSlug(null);
      refresh();
    });

  return (
    <Panel title="Profiles" count={data ? `${data.count} vehicle${data.count === 1 ? "" : "s"}` : undefined}>
      <ResourceGate
        enabled={Boolean(token)}
        enabledMessage="Enter the control token (Control panel) to view and name vehicle profiles."
        loading={loading}
        loadingMessage="loading profiles…"
        error={error}
        data={data}
      >
        {(data) => (
          <>
            <Notice notice={notice} className="mb-2" />
            {data.profiles.length === 0 ? (
              <p className={placeholderClass}>
                No vehicles registered yet. Plug the Pi into a vehicle with the engine
                on; the daemon reads its VIN and registers a profile automatically.
              </p>
            ) : (
              <ul className="space-y-2">
                {data.profiles.map((profile) => {
                  const active = profile.slug === data.active_slug;
                  const editing = editingSlug === profile.slug;
                  return (
                    <li
                      key={profile.slug}
                      className="rounded border border-fg-dim/40 p-2"
                      data-testid={`profile-${profile.slug}`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <div className="min-w-0">
                          <div className="flex items-center gap-2">
                            <span className="truncate font-semibold">
                              {profile.name || <span className={placeholderClass}>(unnamed)</span>}
                            </span>
                            {active && (
                              <span className="rounded bg-ok/20 px-1 text-[0.7rem] text-ok">
                                active
                              </span>
                            )}
                          </div>
                          <div className={cn(placeholderClass, "truncate font-num text-[0.72rem]")}>
                            VIN {profile.vin || "—"} · {profile.slug}
                          </div>
                        </div>
                        {!editing && (
                          <button
                            type="button"
                            className={cn(buttonClass, "shrink-0 text-xs")}
                            onClick={() => startEdit(profile.slug, profile.name)}
                            disabled={busy !== null}
                          >
                            {profile.name ? "Rename" : "Name it"}
                          </button>
                        )}
                      </div>
                      {editing && (
                        <div className="mt-2 flex items-center gap-2">
                          <input
                            type="text"
                            className={cn(inputClass, "flex-1")}
                            value={draftName}
                            placeholder="e.g. 2008 Xterra"
                            aria-label={`Display name for ${profile.slug}`}
                            autoFocus
                            onChange={(e) => setDraftName(e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") onSave(profile.slug);
                              if (e.key === "Escape") setEditingSlug(null);
                            }}
                          />
                          <button
                            type="button"
                            className={cn(buttonClass, "text-xs")}
                            onClick={() => onSave(profile.slug)}
                            disabled={busy !== null}
                          >
                            Save
                          </button>
                          <button
                            type="button"
                            className={cn(buttonClass, "text-xs")}
                            onClick={() => setEditingSlug(null)}
                            disabled={busy !== null}
                          >
                            Cancel
                          </button>
                        </div>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
          </>
        )}
      </ResourceGate>
    </Panel>
  );
}
