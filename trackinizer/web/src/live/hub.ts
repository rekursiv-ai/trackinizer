import type { QueryClient } from "@tanstack/react-query";
import type { StreamListener } from "../api/stream";
import { bootQueries } from "../app/boot";
import type { StreamStatus } from "../ui/bars";
import { Batcher } from "./batcher";
import { RELOAD_OVER } from "./rows";
import { type LiveQuery, Serial } from "./serial";

/** A query's place in the hub: push ids to it directly, or leave. */
export type Registration = {
  /** Queue `ids` for this query alone, such as stale rows that came on screen. */
  readonly push: (ids: Iterable<string>) => void;
  readonly dispose: () => void;
};

/**
 * The stream's side of the app: it batches the stream's ids and hands every
 * batch to every mounted query, each of which applies it against the rows it
 * holds. No query skips an id because another holds it: a row one view shows
 * can still newly match another.
 *
 * Gap recovery: the stream has no resume, so when it opens after a drop, and
 * when a tab hidden for more than 30 s comes back, every query catches up on
 * what the gap may have missed. On the first open only the queries whose reads
 * started before it catch up: the server listens before it says open, so a read
 * started after that misses nothing. A query that registers later, holding a
 * read started before the stream opened (main.tsx's first reads), catches up
 * when it registers.
 * While the tab is hidden nothing is fetched and ids are only collected; on
 * return it catches up once.
 *
 * It also says whether the stream is connected: paused once it has been down
 * for 10 s, connected again when it reopens.
 */
export class LiveHub implements StreamListener {
  readonly #client: QueryClient;
  readonly #batcher: Batcher;
  /** Each registered query, with when the reads it holds started. */
  readonly #serials = new Map<Serial, number>();
  readonly #shared = new Map<string, { serial: Serial; users: number }>();
  #hidden = false;
  #hiddenAt = 0;
  #gapWaiting = false;
  /** When the stream opened, if it is open: changes since then all reach the hub. */
  #openSince: number | null = null;
  /** Whether it has opened before: an open after a drop recovers every query. */
  #reopening = false;
  #status: StreamStatus = "connected";
  #pauseTimer: ReturnType<typeof setTimeout> | null = null;
  #lastInputAt = -Infinity;
  readonly #statusListeners = new Set<() => void>();

  constructor(client: QueryClient) {
    this.#client = client;
    this.#batcher = new Batcher((ids) => this.#deliver(ids, false));
  }

  /**
   * Start keeping `query` current; `dispose` the result when it unmounts.
   *
   * `readAt` is when the reads it holds started (`Date.now()` time), when they
   * started before it mounted: it took one of main.tsx's first reads. Queries
   * registered under one `shareAs` are one query: the first is kept and the
   * others share it until the last leaves. Two views of one inquiry would
   * otherwise each refetch it, the second after joining the first's fetch.
   */
  register(query: LiveQuery, { shareAs, readAt }: { shareAs?: string; readAt?: number } = {}): Registration {
    const shared = shareAs === undefined ? undefined : this.#shared.get(shareAs);
    if (shared) {
      shared.users += 1;
      return { push: shared.serial.push.bind(shared.serial), dispose: once(() => this.#leave(shareAs!)) };
    }
    const serial = new Serial(query, () => !this.#hidden);
    this.#serials.set(serial, readAt ?? Date.now());
    // A query that mounts once the hub has heard the stream open reads after it,
    // unless it took a read that started before.
    if (this.#openSince !== null && readAt !== undefined && readAt <= this.#openSince) this.#recover([serial]);
    if (shareAs === undefined) return { push: serial.push.bind(serial), dispose: once(() => this.#remove(serial)) };
    this.#shared.set(shareAs, { serial, users: 1 });
    return { push: serial.push.bind(serial), dispose: once(() => this.#leave(shareAs)) };
  }

  readonly change = (id: string): void => {
    this.#batcher.add(id);
    // A hidden tab keeps no more ids than a list would check one by one; past
    // that, it recovers the gap on return, as a list over the cap would anyway.
    if (this.#hidden && this.#batcher.size > RELOAD_OVER) {
      this.#batcher.take();
      this.#gapWaiting = true;
    }
  };

  /** The stream opened, at `at` when it opened before the hub heard of it. */
  readonly open = (at: number = Date.now()): void => {
    this.#stopPauseTimer();
    this.#setStatus("connected");
    if (this.#openSince !== null) return;
    this.#openSince = at;
    const serials = [...this.#serials.keys()];
    this.#recover(this.#reopening ? serials : serials.filter((serial) => this.#serials.get(serial)! <= at));
    this.#reopening = true;
  };

  readonly drop = (): void => {
    this.#openSince = null;
    if (this.#pauseTimer === null && this.#status === "connected") {
      this.#pauseTimer = setTimeout(() => {
        this.#pauseTimer = null;
        this.#setStatus("paused");
      }, PAUSED_AFTER_MS);
    }
  };

  /**
   * The server refused the stream. A 401 means the session ended, which the
   * stream cannot tell; refetching the profile finds out, and a 401 there sends
   * the user to sign in as any read does.
   */
  readonly refuse = (): void => {
    void this.#client.invalidateQueries({ queryKey: bootQueries.profile.queryKey });
  };

  /** The tab was hidden: fetch nothing, only collect ids. */
  hide(): void {
    if (this.#hidden) return;
    this.#hidden = true;
    this.#hiddenAt = Date.now();
    this.#batcher.hold();
  }

  /** The tab is back: catch up once, with a gap recovery after more than 30 s away. */
  show(): void {
    if (!this.#hidden) return;
    this.#hidden = false;
    const gap = this.#gapWaiting || Date.now() - this.#hiddenAt > HIDDEN_GAP_MS;
    this.#gapWaiting = false;
    this.#deliver(this.#batcher.release(), gap);
    for (const serial of this.#serials.keys()) serial.resume();
  }

  /** The user did something: a key, a click, a scroll. */
  input(): void {
    this.#lastInputAt = Date.now();
  }

  /** How long since the user last did anything. */
  idleMs(): number {
    return Date.now() - this.#lastInputAt;
  }

  /**
   * Stop the timers and drop what waits; the hub stays usable, as a remount in
   * development needs. The stream is closed, so the next open recovers the gap.
   */
  stop(): void {
    this.#stopPauseTimer();
    this.#batcher.take();
    this.#openSince = null;
  }

  readonly subscribeStatus = (listener: () => void): (() => void) => {
    this.#statusListeners.add(listener);
    return () => this.#statusListeners.delete(listener);
  };

  readonly status = (): StreamStatus => this.#status;

  #leave(shareAs: string): void {
    const shared = this.#shared.get(shareAs)!;
    shared.users -= 1;
    if (shared.users > 0) return;
    this.#shared.delete(shareAs);
    this.#remove(shared.serial);
  }

  #remove(serial: Serial): void {
    serial.dispose();
    this.#serials.delete(serial);
  }

  /** `serials` catch up on a gap; every query gets the ids waiting. A hidden tab recovers them all on return. */
  #recover(serials: readonly Serial[]): void {
    if (serials.length === 0) return;
    if (this.#hidden) this.#gapWaiting = true;
    else this.#deliver(this.#batcher.take(), true, new Set(serials));
  }

  #deliver(ids: ReadonlySet<string>, gap: boolean, gapFor: ReadonlySet<Serial> = new Set(this.#serials.keys())): void {
    if (ids.size === 0 && !gap) return;
    for (const serial of this.#serials.keys()) serial.push(ids, gap && gapFor.has(serial));
  }

  #stopPauseTimer(): void {
    if (this.#pauseTimer !== null) clearTimeout(this.#pauseTimer);
    this.#pauseTimer = null;
  }

  #setStatus(status: StreamStatus): void {
    if (status === this.#status) return;
    this.#status = status;
    for (const listener of this.#statusListeners) listener();
  }
}

/** `run`, taking effect on its first call only, so a second dispose cannot remove another user. */
function once(run: () => void): () => void {
  let done = false;
  return () => {
    if (done) return;
    done = true;
    run();
  };
}

/** How long the stream may be down before the bar says live updates are paused. */
const PAUSED_AFTER_MS = 10_000;

/** A tab hidden longer than this recovers the gap on return: the stream may have been cut meanwhile. */
const HIDDEN_GAP_MS = 30_000;
