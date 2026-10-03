import { act, cleanup, render, screen } from "@testing-library/react";
import { Component, type ReactNode, Suspense } from "react";
import { afterEach, expect, onTestFinished, test, vi } from "vitest";
import { chunk, lazyView, reloadOnChunkError, useLoaded } from "./lazy";

afterEach(() => {
  cleanup();
  sessionStorage.clear();
});

/** Watch for failed chunks as main.tsx does, with `reload` in place of the page's own. */
function install(build: string, storage?: Storage) {
  const reload = vi.fn();
  onTestFinished(reloadOnChunkError({ build, reload, storage }));
  return reload;
}

/** Vite's event for a dynamic import that failed; true when a handler prevented it, so Vite swallows the error. */
function chunkFails(): boolean {
  const event = new Event("vite:preloadError", { cancelable: true });
  dispatchEvent(event);
  return event.defaultPrevented;
}

test("a failed chunk reloads the page once for its build, then lets the error through", () => {
  const reload = install("build-a");
  expect(chunkFails()).toBe(true);
  expect(reload).toHaveBeenCalledTimes(1);
  // The reload brought the same build back, and its chunk fails again: the crash screen shows it.
  expect(chunkFails()).toBe(false);
  expect(reload).toHaveBeenCalledTimes(1);
});

test("a build that a reload brought in reloads again for its own failed chunk", () => {
  const stop = reloadOnChunkError({ build: "build-a", reload: () => {} });
  chunkFails();
  stop();
  const reload = install("build-b");
  expect(chunkFails()).toBe(true);
  expect(reload).toHaveBeenCalledTimes(1);
});

test("without storage, a failed chunk never reloads, since nothing could stop a loop", () => {
  const storage = {
    getItem: () => {
      throw new DOMException("denied", "SecurityError");
    },
  } as unknown as Storage;
  const reload = install("build-a", storage);
  expect(chunkFails()).toBe(false);
  expect(reload).not.toHaveBeenCalled();
});

test("a chunk resolves to its module, and never settles while the page reloads for it", async () => {
  expect(await chunk(Promise.resolve({ value: 1 }))).toEqual({ value: 1 });
  // Vite resolves the import to undefined once a handler prevents its error.
  const settled = vi.fn();
  void chunk(Promise.resolve(undefined)).then(settled, settled);
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(settled).not.toHaveBeenCalled();
});

class Boundary extends Component<{ children: ReactNode }, { error: unknown }> {
  state = { error: null };
  static getDerivedStateFromError(error: unknown) {
    return { error };
  }
  render() {
    return this.state.error ? <p role="alert">{String(this.state.error)}</p> : this.props.children;
  }
}

function show(view: ReactNode) {
  render(
    <Boundary>
      <Suspense fallback={<p>Loading</p>}>{view}</Suspense>
    </Boundary>,
    { onCaughtError: () => {} },
  );
}

/** A chunk that exports `Named`, and a way to see whether it was loaded. */
function fakeChunk() {
  const load = vi.fn(async () => ({ Named: ({ word }: { word: string }) => <p>{word}</p> }));
  return { load, View: lazyView(load, (module) => module.Named) };
}

test("a preloaded view renders at once, without the fallback, and loads its chunk once", async () => {
  const { load, View } = fakeChunk();
  await View.preload();
  show(<View word="loaded" />);
  expect(screen.getByText("loaded")).toBeTruthy();
  expect(screen.queryByText("Loading")).toBeNull();
  await View.preload();
  expect(load).toHaveBeenCalledTimes(1);
});

test("a view not yet loaded shows the fallback, then itself; a chunk that fails reaches the error boundary", async () => {
  const { View } = fakeChunk();
  show(<View word="later" />);
  expect(screen.getByText("Loading")).toBeTruthy();
  await act(async () => {});
  expect(screen.getByText("later")).toBeTruthy();
  cleanup();
  const Failed = lazyView(() => Promise.reject(new TypeError("Failed to fetch dynamically imported module")), () => () => null);
  await act(async () => show(<Failed />));
  expect(screen.getByRole("alert").textContent).toBe("TypeError: Failed to fetch dynamically imported module");
});

test("useLoaded loads a view's chunk while it is needed, and says when it has; a failed load counts", async () => {
  const { load, View } = fakeChunk();
  function Loaded({ needed }: { needed: boolean }) {
    return <output>{String(useLoaded(View, needed))}</output>;
  }
  const view = render(<Loaded needed={false} />);
  await act(async () => {});
  expect(screen.getByRole("status").textContent).toBe("false");
  expect(load).not.toHaveBeenCalled();
  view.rerender(<Loaded needed />);
  await act(async () => {});
  expect(screen.getByRole("status").textContent).toBe("true");
  cleanup();
  const Failed = lazyView(() => Promise.reject(new TypeError("Failed to fetch dynamically imported module")), () => () => null);
  function FailedLoaded() {
    return <output>{String(useLoaded(Failed, true))}</output>;
  }
  render(<FailedLoaded />);
  await act(async () => {});
  expect(screen.getByRole("status").textContent).toBe("true");
});
