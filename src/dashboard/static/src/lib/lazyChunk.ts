import { lazy, memo, type ComponentType, type LazyExoticComponent } from "react";

/** A lazily loaded chunk that could not be fetched.
 *
 *  The usual cause is a tab left open across a Pi update: the new build renamed
 *  every hashed chunk and the old file is gone. `React.lazy` remembers a
 *  rejected load for good, so retrying in place can never work; the recovery
 *  is a reload, which `PanelErrorBoundary` offers for this error. */
export class ChunkLoadError extends Error {
  constructor(cause: unknown) {
    super(`A part of the dashboard could not be loaded (${describe(cause)}).`);
    this.name = "ChunkLoadError";
    this.cause = cause;
  }
}

function describe(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause);
}

/** `React.lazy` for a route-level chunk, with the failure named: every lazy
 *  page goes through it so a failed load reaches the error boundary as a
 *  `ChunkLoadError`, not as whatever the browser's dynamic `import()` threw.
 *  `load` is the dynamic `import()` and `name` the page's named export.
 *  `memoised` wraps the page in `memo` once, at load, for the pages that read
 *  no live channel (see `Workbench`). */
export function lazyChunk<M, K extends keyof M>(
  load: () => Promise<M>,
  name: K,
  { memoised = false }: { memoised?: boolean } = {},
): LazyExoticComponent<ComponentType<PropsOf<M[K]>>> {
  type Page = ComponentType<PropsOf<M[K]>>;
  return lazy(async () => {
    let module: M;
    try {
      module = await load();
    } catch (cause) {
      throw new ChunkLoadError(cause);
    }
    // `memo` returns an exotic component: rendered like any other, typed apart.
    const page = module[name] as unknown as Page;
    return { default: memoised ? (memo(page) as unknown as Page) : page };
  });
}

/** Props of a function component (every lazy page is one). */
type PropsOf<T> = T extends (props: infer P) => unknown ? P : never;
