/** Ids from the stream for one query to apply; `gap` asks it to catch up on what the stream missed. */
export type Batch = { readonly ids: ReadonlySet<string>; readonly gap: boolean };

/** A query kept work for later, such as ids waiting for a membership check: run it again after `afterMs`. */
export type Later = { readonly afterMs: number };

/** Updates that keep failing: the last error, and a way to try again at once. */
export type Failure = { readonly error: Error; readonly retry: () => void };

/** A mounted query the stream keeps current. */
export type LiveQuery = {
  /**
   * Apply `batch`; resolve with `Later` when work is left. Never called while a
   * call is under way. An update that reads more than once asks `open` before
   * each later read and stops when it says no (the tab was hidden): the batch
   * runs again once the tab is back.
   */
  update(batch: Batch, open: () => boolean): Promise<Later | null>;
  /** Hears that updates keep failing, after the first try and two retries; null once one succeeds. */
  readonly failing?: (failure: Failure | null) => void;
};

/**
 * Feed one query its batches, one update at a time.
 *
 * TanStack Query does not queue a second run of a fetch already under way, and
 * a fetch that started before a change can answer without it. So ids that
 * arrive during an update wait here (the dirty flag is this set being non-empty)
 * and the query runs once more when the update ends; none is dropped. A failed
 * update keeps its ids and is tried again after 1, 3 and 10 s, then every 30 s:
 * the reads can fail while the stream stays up, and then nothing else would
 * bring those changes. After the first try and two retries, the query hears of
 * the failure, as a read's error shows after its two retries. Only an update
 * that finishes its work while no failed work waits starts the waits afresh;
 * one that put work off may be about to fail again.
 *
 * An update that kept work for later runs again when it asked, with whatever
 * ids arrived by then, or none; failed work keeps its own wait. Updates run
 * only while `open()` says so (the tab is visible); `resume` starts what waited.
 */
export class Serial {
  readonly #query: LiveQuery;
  readonly #open: () => boolean;
  #ids = new Set<string>();
  #gap = false;
  /** A run put off until now came due, with or without ids. */
  #due = false;
  #running = false;
  #disposed = false;
  #failures = 0;
  /** The time a run was put off to. */
  #later: { at: number; timer: ReturnType<typeof setTimeout> } | null = null;
  /** Failed work, waiting out its backoff. */
  #retry: { ids: Set<string>; gap: boolean; timer: ReturnType<typeof setTimeout> } | null = null;

  constructor(query: LiveQuery, open: () => boolean = () => true) {
    this.#query = query;
    this.#open = open;
  }

  /** Queue `ids`, and a gap recovery when `gap`; run now unless an update is under way. */
  push(ids: Iterable<string>, gap = false): void {
    for (const id of ids) this.#ids.add(id);
    this.#gap ||= gap;
    this.resume();
  }

  /** Run what is queued, if nothing is running and updates may run. */
  resume(): void {
    if (!this.#running && !this.#disposed && this.#open() && this.#queued()) void this.#run();
  }

  /** Stop for good: drop what is queued and any timer. */
  dispose(): void {
    this.#disposed = true;
    if (this.#later) clearTimeout(this.#later.timer);
    if (this.#retry) clearTimeout(this.#retry.timer);
    this.#later = null;
    this.#retry = null;
  }

  async #run(): Promise<void> {
    this.#running = true;
    while (!this.#disposed && this.#open() && this.#queued()) {
      const batch = { ids: this.#ids, gap: this.#gap };
      this.#ids = new Set();
      this.#gap = false;
      this.#due = false;
      try {
        const later = await this.#query.update(batch, this.#open);
        if (!this.#open()) {
          // Hidden meanwhile, the update may have stopped short: all of it runs again on return.
          for (const id of batch.ids) this.#ids.add(id);
          this.#gap ||= batch.gap;
          this.#due = true;
        } else if (later) this.#putOff(later.afterMs);
        else if (!this.#retry) this.#succeeded();
      } catch (error) {
        this.#failed(batch, error instanceof Error ? error : new Error(String(error)));
      }
    }
    this.#running = false;
  }

  #queued(): boolean {
    return this.#ids.size > 0 || this.#gap || this.#due;
  }

  /** Run again after `afterMs`, on one timer that fires at the earliest time asked. */
  #putOff(afterMs: number): void {
    const at = Date.now() + afterMs;
    if (this.#later && this.#later.at <= at) return;
    if (this.#later) clearTimeout(this.#later.timer);
    const timer = setTimeout(() => {
      this.#later = null;
      this.#due = true;
      this.resume();
    }, afterMs);
    this.#later = { at, timer };
  }

  /** Keep `batch` for a new try after the next wait, and say so once the query has failed three times running. */
  #failed(batch: Batch, error: Error): void {
    if (this.#retry) clearTimeout(this.#retry.timer);
    this.#retry = {
      ids: new Set([...(this.#retry?.ids ?? []), ...batch.ids]),
      gap: this.#retry?.gap === true || batch.gap,
      timer: setTimeout(() => this.#retryNow(), RETRY_DELAYS_MS[Math.min(this.#failures, RETRY_DELAYS_MS.length - 1)]),
    };
    this.#failures += 1;
    if (this.#failures >= FAILURES_SHOWN) this.#query.failing?.({ error, retry: () => this.#retryNow() });
  }

  /** Try the failed work now. */
  #retryNow(): void {
    const retry = this.#retry;
    if (!retry) return;
    clearTimeout(retry.timer);
    this.#retry = null;
    // Failed work can have no ids, as a put-off membership check has: it runs regardless.
    this.#due = true;
    this.push(retry.ids, retry.gap);
  }

  #succeeded(): void {
    if (this.#failures >= FAILURES_SHOWN) this.#query.failing?.(null);
    this.#failures = 0;
  }
}

/** The waits before each new try of a failed update; the last repeats. */
const RETRY_DELAYS_MS = [1_000, 3_000, 10_000, 30_000];

/** Failures in a row before the query hears of them: the first try and two retries, after 1 and 3 s. */
const FAILURES_SHOWN = 3;
