import { createContext, type ReactNode, useContext, useState, useSyncExternalStore } from "react";
import { createPortal } from "react-dom";

/** Whether the live stream is delivering changes. */
export type StreamStatus = "connected" | "paused";

/**
 * The live stream's status. The stream layer provides it, and decides when a
 * dropped stream counts as paused; until then the app is taken as connected.
 */
export const StreamStatusContext = createContext<StreamStatus>("connected");

/**
 * Where every bar goes: the shell's one stack of them, null while that stack
 * mounts, or undefined with no stack at all, where a bar draws in place.
 */
const BarStackContext = createContext<HTMLElement | null | undefined>(undefined);

/**
 * The shell's stack of bars, floating over the top of `children`, the view.
 *
 * A bar comes and goes on its own (the network drops, the stream pauses, a
 * refresh fails), so it never takes the view's space: one in the view's flow
 * pushed the rows under the pointer down 34 px (measured). One stack, so bars
 * from the shell and from a view never cover one another.
 */
export function BarStack({ children }: { children: ReactNode }) {
  const [stack, setStack] = useState<HTMLElement | null>(null);
  return (
    <BarStackContext value={stack}>
      <div className="bars" ref={setStack} />
      {children}
    </BarStackContext>
  );
}

/**
 * One bar: a status line, with a button such as Retry when given one. `kind`
 * names it: `offline` (drawn red), `paused`, or `stale` for a failed refresh.
 */
export function Bar({ kind, children }: { kind: "offline" | "paused" | "stale"; children: ReactNode }) {
  const stack = useContext(BarStackContext);
  const bar = (
    <div className={`bar ${kind}`} role="status">
      <span className="bar-dot" aria-hidden="true" />
      {children}
    </div>
  );
  if (stack === undefined) return bar;
  return stack ? createPortal(bar, stack) : null;
}

/** Whether the browser says it is online; follows `online` and `offline` events. */
export function useOnline(): boolean {
  return useSyncExternalStore(subscribeOnline, () => navigator.onLine);
}

/** A bar saying the browser is offline: reads come from the cache, writes wait. */
export function OfflineBar() {
  if (useOnline()) return null;
  return <Bar kind="offline">You are offline. Showing loaded data; changes are off until you reconnect.</Bar>;
}

/** A bar saying live updates are paused while the stream reconnects. */
export function PausedBar() {
  if (useContext(StreamStatusContext) === "connected") return null;
  return <Bar kind="paused">Live updates paused. Reconnecting…</Bar>;
}

function subscribeOnline(onChange: () => void): () => void {
  addEventListener("online", onChange);
  addEventListener("offline", onChange);
  return () => {
    removeEventListener("online", onChange);
    removeEventListener("offline", onChange);
  };
}
