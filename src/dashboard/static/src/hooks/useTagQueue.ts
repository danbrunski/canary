// The in-vehicle tag retry queue, split out of TestSessionPanel. Every
// operator tap is written at click-time and survives cell-signal dropouts
// and a page reload (localStorage), flushed oldest-first one-at-a-time so a
// flaky link never loses or mis-times an event. Logic is unchanged from the
// panel — only lifted into a hook so the panel is just the screen.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { tagCapture } from "../api/http";
import type { CaptureState } from "../types";
import { readJson, writeJson } from "../util/storage";
import { describeError } from "./useControlResource";

type TagStatus = "pending" | "sent" | "failed";

export interface QueuedTag {
  id: string;
  captureId: string;
  label: string;
  kind: string;
  /** Unix seconds at the moment the operator tapped. */
  t: number;
  attempts: number;
  status: TagStatus;
  error?: string;
}

const QUEUE_KEY = "canary.testQueue.v1";
const MAX_RECENT = 40;

interface PersistedQueue {
  queue: QueuedTag[];
  recent: QueuedTag[];
}

function isQueuedTag(value: unknown): value is QueuedTag {
  if (typeof value !== "object" || value === null) return false;
  const row = value as Record<string, unknown>;
  return (
    typeof row.id === "string" &&
    typeof row.captureId === "string" &&
    typeof row.label === "string" &&
    typeof row.t === "number"
  );
}

function tagList(value: unknown): QueuedTag[] {
  return Array.isArray(value) ? value.filter(isQueuedTag) : [];
}

function loadQueue(): PersistedQueue {
  const parsed = readJson<unknown>(QUEUE_KEY, null);
  if (typeof parsed !== "object" || parsed === null) return { queue: [], recent: [] };
  const stored = parsed as Record<string, unknown>;
  return { queue: tagList(stored.queue), recent: tagList(stored.recent) };
}

function saveQueue(state: PersistedQueue): void {
  // A failed write keeps the in-memory queue for this run.
  writeJson(QUEUE_KEY, { queue: state.queue, recent: state.recent.slice(0, MAX_RECENT) });
}

function newId(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

/** Owns the persisted retry queue for the active capture. `flushOnce`
 *  sends the oldest unsent tag for the current capture; the queue auto-
 *  flushes every 2.5 s while anything is pending. */
export function useTagQueue(token: string, capture: CaptureState | null) {
  const [initial] = useState(loadQueue);
  const persisted = useRef<PersistedQueue>(initial);
  const [queue, setQueue] = useState<QueuedTag[]>(initial.queue);
  const [recent, setRecent] = useState<QueuedTag[]>(initial.recent);
  const flushing = useRef(false);

  const pendingCount = queue.filter((q) => q.status !== "sent").length;

  const persist = useCallback((q: QueuedTag[], r: QueuedTag[]) => {
    persisted.current = { queue: q, recent: r };
    saveQueue(persisted.current);
  }, []);

  const flushOnce = useCallback(async (): Promise<void> => {
    if (flushing.current || !token) return;
    const cap = capture;
    if (!cap) return;
    flushing.current = true;
    try {
      // Oldest first, one at a time — preserves order, won't hammer a flaky link.
      const next = persisted.current.queue.find(
        (q) => q.status !== "sent" && q.captureId === cap.capture_id,
      );
      if (!next) return;
      try {
        await tagCapture(token, {
          label: next.label,
          kind: next.kind,
          timestamp: next.t,
          fields: {},
        });
        const work = persisted.current.queue.filter((q) => q.id !== next.id);
        const rec = persisted.current.recent.map((r) =>
          r.id === next.id ? { ...r, status: "sent" as const } : r,
        );
        persist(work, rec);
        setQueue(work);
        setRecent(rec);
      } catch (err) {
        const work = persisted.current.queue.map((q) =>
          q.id === next.id
            ? {
                ...q,
                attempts: q.attempts + 1,
                status: "failed" as const,
                error: describeError(err),
              }
            : q,
        );
        persist(work, persisted.current.recent);
        setQueue(work);
      }
    } finally {
      flushing.current = false;
    }
  }, [token, capture, persist]);

  useEffect(() => {
    if (pendingCount === 0 || !capture) return;
    flushOnce().catch(() => undefined);
    const timer = window.setInterval(() => void flushOnce().catch(() => undefined), 2500);
    return () => window.clearInterval(timer);
  }, [pendingCount, capture, flushOnce]);

  const enqueue = useCallback(
    (label: string, kind: string) => {
      if (!capture) return;
      const row: QueuedTag = {
        id: newId(),
        captureId: capture.capture_id,
        label,
        kind,
        t: Date.now() / 1000,
        attempts: 0,
        status: "pending",
      };
      const q = [...persisted.current.queue, row];
      const r = [row, ...persisted.current.recent].slice(0, MAX_RECENT);
      persist(q, r);
      setQueue(q);
      setRecent(r);
      flushOnce().catch(() => undefined);
    },
    [capture, persist, flushOnce],
  );

  // Per-tag tap counts for the current capture, derived from the persisted
  // recent list so the tapped state survives panel navigation and reload.
  const tapCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const r of recent) {
      if (r.captureId === capture?.capture_id) {
        counts.set(r.label, (counts.get(r.label) ?? 0) + 1);
      }
    }
    return counts;
  }, [recent, capture?.capture_id]);

  // Reads the live persisted queue (React state is stale inside a loop).
  const stillPending = useCallback(
    () => persisted.current.queue.some((q) => q.status !== "sent"),
    [],
  );

  const unsentCount = useCallback(
    () => persisted.current.queue.filter((q) => q.status !== "sent").length,
    [],
  );

  const clearOrphans = useCallback(() => {
    const kept = persisted.current.queue.filter(
      (q) => capture && q.captureId === capture.capture_id && q.status !== "sent",
    );
    persist(kept, persisted.current.recent);
    setQueue(kept);
  }, [capture, persist]);

  /** Drop the whole queue (keep the recent log) — used after a clean stop. */
  const clearQueue = useCallback(() => {
    persist([], persisted.current.recent);
    setQueue([]);
  }, [persist]);

  const orphans = queue.filter(
    (q) => q.status !== "sent" && (!capture || q.captureId !== capture.capture_id),
  ).length;

  return {
    queue,
    recent,
    pendingCount,
    tapCounts,
    orphans,
    enqueue,
    flushOnce,
    stillPending,
    unsentCount,
    clearOrphans,
    clearQueue,
  };
}
