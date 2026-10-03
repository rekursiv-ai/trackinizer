import type { QueryClient } from "@tanstack/react-query";
import { type ChangeKind, type LoggedChange, listChanges } from "../api/changes";
import { activityQueries } from "../activity/queries";
import { readOnce, refetchFresh } from "./cache";
import type { Batch, Later } from "./serial";

/**
 * The Activity feed, kept current by the stream (the plan's rule for it).
 *
 * A batch never says what kind of change it carries, so the feed asks for its
 * tab's changes since the newest it shows, every kind of the tab in one request
 * (one for All too), at most once every 2 s. New changes join the top of the
 * feed: they go in the tab's head (`activityQueries.head`), ahead of its first
 * page, whose rows and paging stay as the server sent them. A full answer may
 * have skipped changes, and the head holds no more than a page, so past either,
 * the tab reloads its first page instead and starts a new head, as a gap
 * recovery does. A batch that comes while the first page is being read waits
 * for it, since that read may predate the batch.
 *
 * A batch does not read again the lookups of the inquiries lines name, for
 * their titles: with them, a tab made 3.5 requests a second at today's event
 * rate (measured when each kind asked on its own), over the 3 it may make.
 * Titles are read afresh when Activity opens again and after the user's own
 * writes.
 */
export class ActivityLive {
  readonly #client: QueryClient;
  readonly #kinds: readonly ChangeKind[];
  readonly #now: () => number;
  #lastAskAt = -Infinity;

  constructor(client: QueryClient, kinds: readonly ChangeKind[], now: () => number = Date.now) {
    this.#client = client;
    this.#kinds = kinds;
    this.#now = now;
  }

  async update({ gap }: Batch): Promise<Later | null> {
    if (gap) {
      await this.#reload();
      return null;
    }
    // A first page's read under way may predate the change: ask once it has loaded.
    if (this.#firstPageLoading()) return { afterMs: FIRST_PAGE_WAIT_MS };
    const wait = this.#lastAskAt + ASK_EVERY_MS - this.#now();
    if (wait > 0) return { afterMs: wait };
    this.#lastAskAt = this.#now();
    if (this.#firstPage()) await this.#ask();
    return null;
  }

  /** Ask for the changes since the newest shown (`since` is inclusive, so that one comes back too). */
  async #ask(): Promise<void> {
    const kinds = this.#kinds;
    const since = (this.#head()[0] ?? this.#firstPage()![0])?.created;
    const rows = await readOnce(this.#client, ["activity", kinds, since], (signal) =>
      listChanges({ kind: kinds, since, limit: ASK_LIMIT, brief: true }, { signal }),
    );
    const head = this.#head();
    const shown = new Set([...head, ...(this.#firstPage() ?? [])].map((row) => row.id));
    const fresh = rows.filter((row) => !shown.has(row.id));
    if (rows.length >= ASK_LIMIT || head.length + fresh.length > ASK_LIMIT) return this.#reload();
    if (fresh.length > 0) this.#client.setQueryData(activityQueries.head(kinds).queryKey, [...fresh, ...head]);
  }

  async #reload(): Promise<void> {
    await refetchFresh(this.#client, { type: "active", queryKey: activityQueries.page(this.#kinds, null).queryKey, exact: true });
    this.#client.setQueryData(activityQueries.head(this.#kinds).queryKey, []);
  }

  #firstPage(): readonly LoggedChange[] | undefined {
    return this.#client.getQueryData(activityQueries.page(this.#kinds, null).queryKey);
  }

  /** Whether the first page has no rows yet and a read of it is under way. */
  #firstPageLoading(): boolean {
    const state = this.#client.getQueryState(activityQueries.page(this.#kinds, null).queryKey);
    return state !== undefined && state.data === undefined && state.fetchStatus !== "idle";
  }

  #head(): readonly LoggedChange[] {
    return this.#client.getQueryData(activityQueries.head(this.#kinds).queryKey) ?? [];
  }
}

/** The least time between two asks, the plan's "at most every 2 s". */
const ASK_EVERY_MS = 2_000;

/** The most changes one ask takes, and the head holds: a page, as the feed loads them. */
const ASK_LIMIT = 50;

/** How often a batch that came before the first pages looks again (a page takes about 0.15 s). */
const FIRST_PAGE_WAIT_MS = 500;
