/**
 * Collect stream ids for one window and hand them over once, deduplicated.
 *
 * The first id of a quiet spell starts the window and `flush` runs at its end,
 * so a burst costs one batch however many frames it holds (a 20-second sample
 * of the stream had 64 frames but 6 distinct ids). While held (a hidden tab),
 * ids are only collected; `release` hands them all over at once.
 */
export class Batcher {
  readonly #flush: (ids: ReadonlySet<string>) => void;
  readonly #windowMs: number;
  #ids = new Set<string>();
  #timer: ReturnType<typeof setTimeout> | null = null;
  #held = false;

  constructor(flush: (ids: ReadonlySet<string>) => void, windowMs = 1_000) {
    this.#flush = flush;
    this.#windowMs = windowMs;
  }

  /** How many ids are collected and not yet flushed. */
  get size(): number {
    return this.#ids.size;
  }

  /** Collect `id`; start the window if none is open. */
  add(id: string): void {
    this.#ids.add(id);
    if (this.#timer === null && !this.#held) this.#timer = setTimeout(() => this.#flush(this.take()), this.#windowMs);
  }

  /** Stop flushing; keep collecting. */
  hold(): void {
    this.#held = true;
    this.#stop();
  }

  /** Flush again from now on; returns what was collected meanwhile. */
  release(): ReadonlySet<string> {
    this.#held = false;
    return this.take();
  }

  /** Every id collected and not yet flushed; the open window closes without a flush. */
  take(): ReadonlySet<string> {
    this.#stop();
    const ids = this.#ids;
    this.#ids = new Set();
    return ids;
  }

  #stop(): void {
    if (this.#timer !== null) clearTimeout(this.#timer);
    this.#timer = null;
  }
}
