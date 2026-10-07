// The live stream, opened by the entry chunk (src/main.tsx) before the first
// reads, so that it is open by the time the views mount and read: the server
// listens before it says open, so a read that starts after the open misses no
// change, and needs no gap recovery (LiveHub). Opened by the app instead, it
// opened only after the views had read, and every one of them read again.
import { openStream, type StreamListener } from "../api/stream";

/** A stream listener that hears, on an open it heard of late, when the stream opened. */
export type OpenListener = Omit<StreamListener, "open"> & { readonly open: (at?: number) => void };

/**
 * Open the live stream now, for the app's live layer to take when it mounts
 * (`attachStream`).
 *
 * Until then it keeps only when it opened. A change it hears meanwhile is
 * dropped: a read a view starts on mounting starts after it and sees it, and a
 * view that took an earlier read (`prefetch`) catches up on the gap instead.
 */
export function openEarlyStream(): void {
  let listener: OpenListener | null = null;
  let openedAt: number | null = null;
  const close = openStream({
    open: () => (listener ? listener.open() : (openedAt = Date.now())),
    change: (id) => listener?.change(id),
    drop: () => (listener ? listener.drop() : (openedAt = null)),
    refuse: () => listener?.refuse(),
  });
  early = {
    close,
    attach: (attached) => {
      listener = attached;
      if (openedAt !== null) attached.open(openedAt);
    },
  };
}

/**
 * Close the stream main.tsx opened, for a user whose live layer takes its ids
 * from the canvas's stream instead (`CanvasStream`).
 */
export function closeEarlyStream(): void {
  early?.close();
  early = null;
}

/**
 * Hand the live stream to `listener`: the one main.tsx opened, the first time;
 * otherwise a stream of its own, as in a remount. Returns the function that
 * closes it.
 */
export function attachStream(listener: OpenListener): () => void {
  const taken = early;
  early = null;
  if (!taken) return openStream(listener);
  taken.attach(listener);
  return taken.close;
}

/** The stream main.tsx opened, until the live layer takes it. */
let early: { readonly close: () => void; readonly attach: (listener: OpenListener) => void } | null = null;
