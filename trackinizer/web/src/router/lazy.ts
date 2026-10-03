// Code the app loads as chunks of their own, and what a tab does when a chunk it
// asks for is gone.
import { type ComponentType, createElement, lazy, useEffect, useState } from "react";
import { log } from "../debug/log";

/** A view whose code loads the first time it renders, or sooner through `preload`. */
export type LazyView<P> = ComponentType<P> & {
  /** Start loading the view's chunk, once; the promise settles when it has. */
  readonly preload: () => Promise<void>;
};

/**
 * `load`'s module, as a dynamic import resolves it; a promise that never settles
 * while the page reloads for the chunk instead (`reloadOnChunkError`), for Vite
 * then resolves the import to undefined.
 */
export async function chunk<M>(load: Promise<M>): Promise<M> {
  const module: M | undefined = await load;
  return module ?? new Promise<never>(() => {});
}

/**
 * A view whose code is a chunk of its own: `load` imports it, and `pick` names the
 * component in it.
 *
 * Until the chunk has loaded it renders as React's `lazy` does, under the nearest
 * `Suspense`, and a chunk that fails reaches the nearest error boundary. Once
 * loaded, by an earlier render or by `preload`, it renders at once: a first
 * render that suspends shows the fallback for at least React's 300 ms throttle,
 * however fast the chunk arrives. That is react-lazy-with-preload's design
 * (https://github.com/ianschmitz/react-lazy-with-preload).
 */
export function lazyView<M, P extends object>(load: () => Promise<M>, pick: (module: M) => ComponentType<P>): LazyView<P> {
  let loaded: ComponentType<P> | undefined;
  let loading: Promise<ComponentType<P>> | undefined;
  const component = () => (loading ??= chunk(load()).then((module) => (loaded = pick(module))));
  const Lazy = lazy(async () => ({ default: await component() }));
  function View(props: P) {
    // Chosen once: switching from the lazy component to the loaded one on a later
    // render would remount the view and lose its state.
    const [Shown] = useState<ComponentType<P>>(() => loaded ?? Lazy);
    return createElement(Shown, props);
  }
  return Object.assign(View, {
    preload: async () => {
      await component();
    },
  });
}

/**
 * Whether `view`'s chunk has loaded, loading it while `needed`. A load that
 * failed counts as loaded: the view reports it when it renders.
 *
 * A view rendered only once this is true never suspends, so it skips the 300 ms
 * React holds a suspended view's fallback on screen, however soon its chunk comes.
 */
export function useLoaded(view: { readonly preload: () => Promise<void> }, needed: boolean): boolean {
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    if (!needed) return;
    let mounted = true;
    void view
      .preload()
      .catch(() => {})
      .then(() => {
        if (mounted) setLoaded(true);
      });
    return () => {
      mounted = false;
    };
  }, [view, needed]);
  return loaded;
}

/**
 * Run `task` once the browser is idle, or at the latest after 2 s; a browser
 * without `requestIdleCallback` (Safari) runs it after the current task.
 * Returns the function that cancels it.
 */
export function whenIdle(task: () => void): () => void {
  if (typeof requestIdleCallback === "function") {
    const handle = requestIdleCallback(task, { timeout: 2000 });
    return () => cancelIdleCallback(handle);
  }
  const handle = setTimeout(task, 0);
  return () => clearTimeout(handle);
}

/**
 * Reload the page when a lazily loaded chunk fails, once per build.
 *
 * A deploy points `current` at a new build, and an open tab still asks for its
 * old build's chunks, which are gone. Vite dispatches `vite:preloadError` for
 * each failed dynamic import, and its docs reload the page on it
 * (https://vite.dev/guide/build#load-error-handling): the reload fetches the new
 * build. The build that reloaded is kept in this tab's `sessionStorage`, so when
 * the same build's chunk fails again (the server is down, or the chunk is
 * missing from it), the page does not reload again but lets the error through,
 * to the crash screen and its Copy details. Without storage it never reloads,
 * since nothing would then stop a loop.
 *
 * `build` defaults to the commit the page was built from; `reload` and `storage`
 * are the page's own. Returns the function that stops it.
 */
export function reloadOnChunkError({
  build = __COMMIT__,
  reload = () => location.reload(),
  storage,
}: {
  build?: string;
  reload?: () => void;
  storage?: Storage;
} = {}): () => void {
  const onError = (event: Event) => {
    try {
      const store = storage ?? sessionStorage;
      if (store.getItem(RELOADED_KEY) === build) return;
      store.setItem(RELOADED_KEY, build);
    } catch {
      return;
    }
    log("warn", "chunk.reload", { build });
    event.preventDefault();
    reload();
  };
  addEventListener("vite:preloadError", onError);
  return () => removeEventListener("vite:preloadError", onError);
}

/** The build that last reloaded this tab for a failed chunk. */
const RELOADED_KEY = "trackinizer.v2.chunk_reload";
