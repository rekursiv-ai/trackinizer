import type { QueryClient } from "@tanstack/react-query";
import { type FeedCursor, type FeedEvent, type FeedFilters, type FeedPage, type FeedRead, readFeed, type SessionRecord } from "../api/sessions";
import type { Batch, Later } from "../live/serial";
import { type Level, levelOf } from "./levels";

/** Records a read asks for, as the old console's did. */
const PAGE = 300;

/** Records the console holds; past them the oldest drop, as the old console's buffer did. */
const MAX_HELD = 5000;

/** Records the live tail's first read looks for that the view's level shows, about two screens. */
const FILL = 50;

/** Pages the live tail's first read takes at most, looking for them. */
const FILL_PAGES = 5;

/**
 * One feed record as the console holds it: its place in arrival order (`n`,
 * which survives older records dropping), its key, the event, and the event as a
 * transcript record, for the transcript's views.
 */
export type Held = { readonly n: number; readonly key: string; readonly event: FeedEvent; readonly record: SessionRecord };

/** What the console follows: the live tail, or a window of history (ISO times; either end may be open). */
export type Range = { readonly live: true } | { readonly live: false; readonly since: string | null; readonly until: string | null };

/** What the console has read: its records, whether the first read is under way or failed, and whether more may follow. */
export type FeedState = {
  readonly held: readonly Held[];
  readonly loading: boolean;
  readonly error: Error | null;
  /** History only: the last read was a full page, so the window may hold more. */
  readonly more: boolean;
};

/**
 * `held` with `events` after it, each once: a record is its session, part and
 * seq (seq restarts in each part). Past `MAX_HELD`, the oldest drop. Nothing new
 * gives back `held` itself.
 */
export function appendEvents(held: readonly Held[], events: readonly FeedEvent[]): readonly Held[] {
  const seen = new Set(held.map(({ key }) => key));
  let n = held.at(-1)?.n ?? -1;
  const fresh: Held[] = [];
  for (const event of events) {
    const key = keyOf(event);
    if (seen.has(key)) continue;
    seen.add(key);
    fresh.push({ n: ++n, key, event, record: asRecord(event) });
  }
  if (!fresh.length) return held;
  return [...held, ...fresh].slice(-MAX_HELD);
}

/**
 * The cross-session feed the console shows (`GET /api/web/feed`), as a store a
 * view subscribes to: the records `filter` lets through, and at Messages only
 * the conversation. Live, it reads the newest page, and older ones while they
 * hold few records `level` shows; then, whenever the stream says something
 * changed, every page past its cursor until a short one: a captured record
 * wakes the stream with its session's id. After a gap in the stream it reads
 * the newest page again too, for records written again. History reads the
 * window's first page, and the next on request.
 */
export class ConsoleFeed {
  readonly #client: QueryClient;
  readonly #range: Range;
  readonly #filter: FeedFilters;
  readonly #level: Level;
  #state: FeedState = { held: [], loading: true, error: null, more: false };
  #cursor: FeedCursor | null = null;
  readonly #listeners = new Set<() => void>();

  constructor(client: QueryClient, range: Range, filter: FeedFilters, level: Level) {
    this.#client = client;
    this.#range = range;
    this.#filter = filter;
    this.#level = level;
  }

  readonly subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  };

  readonly getSnapshot = (): FeedState => this.#state;

  /** Read the first page: the newest, live (`#tail`); the window's oldest, history. A failure is kept for the view to show. */
  async load(): Promise<void> {
    this.#set({ ...this.#state, loading: true, error: null });
    try {
      this.#take(await (this.#range.live ? this.#tail() : this.#read(this.#window(), true)));
      this.#set({ ...this.#state, loading: false });
    } catch (error) {
      this.#set({ ...this.#state, loading: false, error: asError(error) });
    }
  }

  /** History: read the window's next page. A failure is kept for the view to show. */
  async more(): Promise<void> {
    try {
      this.#take(await this.#read(this.#window(), true));
      this.#set({ ...this.#state, error: null });
    } catch (error) {
      this.#set({ ...this.#state, error: asError(error) });
    }
  }

  /**
   * Live: read past the cursor until a short page; after a gap, the newest page
   * again too, whose records replace those held under the same keys, since the
   * stream may have missed a restart writing them again. While the first page is
   * being read, wait for it, since it may predate the change; after it failed,
   * there is nothing to read past until the view's Retry loads it again.
   */
  async update({ gap }: Batch): Promise<Later | null> {
    if (this.#state.loading) return { afterMs: 1000 };
    if (this.#state.error && !this.#state.held.length) return null;
    let page: FeedPage;
    do {
      page = await this.#read(this.#window(), false);
      this.#take(page);
    } while (page.events.length >= PAGE);
    if (gap) {
      const newest = await this.#read({ tail: true, limit: PAGE }, false);
      this.#set({ ...this.#state, held: replaceEvents(this.#state.held, newest.events) });
    }
    return null;
  }

  /**
   * The newest page, its records joined by older pages while they hold fewer
   * than `FILL` that the level shows, up to `FILL_PAGES`: + Calls hides context
   * records and + Output bookkeeping, either of which can fill a page. Messages
   * reads only what it shows, so its newest page is enough. The cursor stays the
   * newest page's.
   */
  async #tail(): Promise<FeedPage> {
    const newest = await this.#read({ tail: true, limit: PAGE }, true);
    let [page, events] = [newest, newest.events];
    for (let pages = 1; pages < FILL_PAGES && page.events.length >= PAGE && this.#shown(events) < FILL; pages++) {
      // `until` holds its own instant, so a page cut inside a same-instant group loses none of it.
      page = await this.#read({ tail: true, until: events[0]!.created, limit: PAGE }, true);
      events = [...page.events, ...events];
    }
    return { ...newest, events };
  }

  #shown(events: readonly FeedEvent[]): number {
    return events.filter((event) => levelOf(event) <= this.#level).length;
  }

  /** The next read past the cursor, within the window when there is one. */
  #window(): FeedRead {
    const range = this.#range;
    return {
      ...(this.#cursor ? { after: this.#cursor } : {}),
      ...(!range.live && range.since ? { since: range.since } : {}),
      ...(!range.live && range.until ? { until: range.until } : {}),
      limit: PAGE,
    };
  }

  /**
   * One read through the query cache, so a 401 ends the session as any read's
   * does; `retry` as reads do, or not, when the stream layer retries it instead.
   * Messages reads `conversation=true` in place of its kinds, which it implies:
   * beside one kind alone the server digs far for a page (0.8 s on 9 million
   * records).
   */
  #read(read: FeedRead, retry: boolean): Promise<FeedPage> {
    const filtered = { ...read, ...this.#filter, ...(this.#level === 1 ? { kind: [], conversation: true } : {}) };
    return this.#client.fetchQuery({
      queryKey: ["console", "feed", filtered],
      queryFn: ({ signal }) => readFeed(filtered, { signal }),
      staleTime: 0,
      gcTime: 0,
      ...(retry ? {} : { retry: false }),
    });
  }

  #take(page: FeedPage): void {
    this.#cursor = page.next_after ?? this.#cursor;
    this.#set({ ...this.#state, held: appendEvents(this.#state.held, page.events), more: page.events.length >= PAGE });
  }

  #set(state: FeedState): void {
    this.#state = state;
    for (const listener of this.#listeners) listener();
  }
}

/** `held` with each record of `events` in place of the one held under its key, keeping its place `n`. */
function replaceEvents(held: readonly Held[], events: readonly FeedEvent[]): readonly Held[] {
  const again = new Map(events.map((event) => [keyOf(event), event]));
  return held.map((one) => {
    const event = again.get(one.key);
    return event ? { ...one, event, record: asRecord(event) } : one;
  });
}

/** A record's key: its session, part and seq (seq restarts in each part). */
function keyOf(event: FeedEvent): string {
  return `${event.session_id}:${event.part}:${event.seq}`;
}

/** `event` as a transcript record, for the transcript's views. The feed names its payload `message`. */
function asRecord(event: FeedEvent): SessionRecord {
  return {
    idx: event.seq,
    kind: event.kind,
    context_id: null,
    timestamp: event.timestamp ?? null,
    model: event.model ?? null,
    payload: event.message,
    text: event.text ?? "",
    ciphertext: null,
  };
}

function asError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}
