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
  close(): void;
};

/** Options for `openStream`; tests replace the connection and the delays. */
export type StreamOptions = {
  /** Open a connection to `url`; the browser's `EventSource` by default. */
  readonly connect?: (url: string) => EventSourceLike;
  /** How long to wait before each new try after a refusal, the last repeating. */
  readonly retryDelaysMs?: readonly number[];
};

/**
 * Listen to `GET /api/web/subscribe`: one frame per `change_log` row, each
 * `data: {"id": "<subject uuid>"}` with no event name or resume, plus comment
 * lines (on open and every 25 s idle) that `EventSource` drops before
 * `onmessage`. Returns a function that closes the stream for good.
 *
 * `EventSource` reconnects by itself after a dropped connection, so an error
 * never closes the stream here: the old UI closed it on every error and never
 * recovered. Only when the browser gives up (the server answered with anything
 * but a stream) does the wrapper open a new one, after a delay.
 */
export function openStream(listener: StreamListener, options: StreamOptions = {}): () => void {
  const connect = options.connect ?? ((url: string) => new EventSource(url));
  const delays = options.retryDelaysMs ?? RETRY_DELAYS_MS;
  let source: EventSourceLike | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let refusals = 0;
  let closed = false;
  const start = () => {
    timer = null;
    if (closed) return;
    const opened = connect(new URL(SUBSCRIBE_PATH, globalThis.location.origin).href);
    source = opened;
    opened.onopen = () => {
      refusals = 0;
      listener.open();
    };
    opened.onmessage = (event) => {
      const id = subjectId(event.data);
      // Logged, not thrown: one bad frame must not stop the stream, but a silent
      // skip would hide the server's frame shape drifting from this parser.
      if (id === null) console.warn("Ignored a live-stream frame without an id.", event.data);
      else listener.change(id);
    };
    opened.onerror = () => {
      if (closed) return;
      listener.drop();
      if (opened.readyState !== CLOSED) return;
      // A closed source is done: one refusal, one new try, whatever it fires next.
      opened.onerror = null;
      listener.refuse();
      timer = setTimeout(start, delays[Math.min(refusals, delays.length - 1)]);
      refusals += 1;
    };
  };
  start();
  return () => {
    closed = true;
    if (timer !== null) clearTimeout(timer);
    timer = null;
    source?.close();
  };
}

/** The route, checked against the schema so a renamed route fails `tsc`. */
const SUBSCRIBE_PATH = "/api/web/subscribe" satisfies keyof paths;

/** `EventSource.CLOSED`, which a fake need not define. */
const CLOSED = 2;

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
