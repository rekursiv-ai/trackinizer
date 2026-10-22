import type { paths } from "./generated/schema";

/** What the live stream tells its listener. */
export type StreamListener = {
  /** A `change_log` row was written about the inquiry `id`. */
  readonly change: (id: string) => void;
  /** The stream is connected: the first time, and after every reconnect. */
  readonly open: () => void;
  /** The stream dropped. It reconnects on its own; this can repeat while it tries. */
  readonly drop: () => void;
  /**
   * The server refused the stream (any answer but a stream, such as a 401 or a
   * proxy's 502), so the browser gave up on it. The wrapper tries again later.
   */
  readonly refuse: () => void;
};

/** The part of `EventSource` the wrapper uses, so tests can pass a fake. */
export type EventSourceLike = {
  readonly readyState: number;
  onopen: ((event: Event) => void) | null;
  onmessage: ((event: MessageEvent) => void) | null;
  onerror: ((event: Event) => void) | null;
  /** The server's keep-alive, `event: ping`; `onmessage` never sees it. */
  addEventListener(type: "ping", listener: () => void): void;
  close(): void;
};

/** Options for `openStream`; tests replace the connection and the delays. */
export type StreamOptions = {
  /** Open a connection to `url`; the browser's `EventSource` by default. */
  readonly connect?: (url: string) => EventSourceLike;
  /** How long to wait before each new try after a refusal, the last repeating. */
  readonly retryDelaysMs?: readonly number[];
};

/** What `openEvents` tells its listener, besides each frame's data. */
export type EventsListener = Pick<StreamListener, "open" | "drop" | "refuse"> & {
  /** One frame's `data`, as the server wrote it. */
  readonly data: (data: unknown) => void;
};

/**
 * Listen to `GET /api/web/subscribe`: one frame per `change_log` row, each
 * `data: {"id": "<subject uuid>"}` with no event name or resume, plus a
 * `ping` event every 25 s idle. Returns a function that closes the stream for good.
 */
export function openStream(listener: StreamListener, options: StreamOptions = {}): () => void {
  return openEvents(SUBSCRIBE_PATH, {
    open: listener.open,
    drop: listener.drop,
    refuse: listener.refuse,
    data: (data) => {
      const id = subjectId(data);
      // Logged, not thrown: one bad frame must not stop the stream, but a silent
      // skip would hide the server's frame shape drifting from this parser.
      if (id === null) console.warn("Ignored a live-stream frame without an id.", data);
      else listener.change(id);
    },
  }, options);
}

/**
 * Listen to one server-sent-events route at `path` and keep it open.
 *
 * `EventSource` reconnects by itself after a dropped connection, so an error
 * never closes the stream here: the old UI closed it on every error and never
 * recovered. Only when the browser gives up (the server answered with anything
 * but a stream) does the wrapper open a new one, after a delay.
 *
 * A tunnel can also drop a connection without the browser seeing an error. The
 * server speaks at least every 25 s (a frame or a `ping` event), so a source that
 * has heard nothing for `SILENCE_MS` is dead: the wrapper tells the listener it
 * dropped, closes it and opens a new one after the same delays. Returns a
 * function that closes the stream for good.
 */
export function openEvents(path: string, listener: EventsListener, options: StreamOptions = {}): () => void {
  const connect = options.connect ?? ((url: string) => new EventSource(url));
  const delays = options.retryDelaysMs ?? RETRY_DELAYS_MS;
  let source: EventSourceLike | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let refusals = 0;
  let silence: ReturnType<typeof setTimeout> | null = null;
  let closed = false;
  const later = () => {
    timer = setTimeout(start, delays[Math.min(refusals, delays.length - 1)]);
    refusals += 1;
  };
  const start = () => {
    timer = null;
    if (closed) return;
    const opened = connect(new URL(path, globalThis.location.origin).href);
    source = opened;
    // Whatever the server sends restarts the wait; so does connecting.
    const heard = () => {
      if (silence !== null) clearTimeout(silence);
      silence = setTimeout(() => {
        silence = null;
        opened.onerror = null;
        opened.close();
        listener.drop();
        later();
      }, SILENCE_MS);
    };
    heard();
    opened.onopen = () => {
      refusals = 0;
      heard();
      listener.open();
    };
    opened.onmessage = (event) => {
      heard();
      listener.data(event.data);
    };
    opened.addEventListener("ping", heard);
    opened.onerror = () => {
      if (closed) return;
      listener.drop();
      if (opened.readyState !== CLOSED) return;
      // A closed source is done: one refusal, one new try, whatever it fires next.
      opened.onerror = null;
      if (silence !== null) clearTimeout(silence);
      silence = null;
      listener.refuse();
      later();
    };
  };
  start();
  return () => {
    closed = true;
    if (timer !== null) clearTimeout(timer);
    if (silence !== null) clearTimeout(silence);
    timer = null;
    silence = null;
    source?.close();
  };
}

/** The route, checked against the schema so a renamed route fails `tsc`. */
const SUBSCRIBE_PATH = "/api/web/subscribe" satisfies keyof paths;

/** `EventSource.CLOSED`, which a fake need not define. */
const CLOSED = 2;

/**
 * How long a source may hear nothing before it counts as dead: 2.5 times the
 * server's 25 s keep-alive, so one late or lost ping does not cut a live stream.
 */
const SILENCE_MS = 62_500;

const RETRY_DELAYS_MS = [1_000, 3_000, 10_000, 30_000];

/** The subject id in one frame's data, or null for a frame of any other shape. */
function subjectId(data: unknown): string | null {
  if (typeof data !== "string") return null;
  let frame: unknown;
  try {
    frame = JSON.parse(data);
  } catch {
    return null;
  }
  const id = typeof frame === "object" && frame !== null ? (frame as { id?: unknown }).id : undefined;
  return typeof id === "string" ? id : null;
}
