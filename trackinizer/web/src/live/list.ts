import type { Query, QueryClient } from "@tanstack/react-query";
import { INQUIRY_ROW_FIELDS, type InquiryRow, inquiryKinds, listInquiries, listInquiriesBySeq } from "../api/inquiries";
import { serverText } from "../lists/pages";
import type { Filter, ListRequest } from "../query/query";
import { readOnce } from "./cache";
import {
  CHECK_IDS,
  heldRows,
  idFilter,
  isPageOf,
  newerRow,
  type Page,
  pageOf,
  placeRow,
  RELOAD_OVER,
  seqRanges,
  serverOrder,
  WHOLE_IDS,
} from "./rows";
import type { Batch, Failure, Later } from "./serial";

/** Which of a list's rows are on screen now. */
export type RowsOnScreen = { onScreen(ids: Iterable<string>): ReadonlySet<string> };

/** What a list's screen shows of its live state. */
export type ListSnapshot = {
  /** Rows that entered the list and wait for the "N new" pill or an idle list at its top. */
  readonly arrivals: number;
  /** Rows the list shows that no longer match it; they stay, dimmed, until merged away. */
  readonly left: ReadonlySet<string>;
  /** Rows that entered and were merged in: shown with the loaded rows. */
  readonly joined: ReadonlyMap<string, InquiryRow>;
  /** Loaded rows that stopped matching and were merged away: no longer shown. */
  readonly gone: ReadonlySet<string>;
  /** Live updates keep failing, so the rows may be stale. */
  readonly failure: Failure | null;
};

/**
 * One mounted list, kept current by the stream (steps 1 to 3 in the plan's
 * "Live updates").
 *
 * The rows it holds are those of its own mounted pages in the query cache (its
 * filters, kinds and page size), read at each update, and the rows merged into
 * it. For every batch:
 *
 * 1. Rows it holds that are on screen refetch, with the list's own kinds and
 *    filters plus a `seq_range` of just those rows. A row that comes back is
 *    updated in place; one that does not no longer matches, and is marked left.
 * 2. Rows it holds that are off screen are marked stale, and refetch when they
 *    come on screen.
 * 3. Ids it does not hold wait for a membership check: the list's kinds and
 *    filters plus an `id` filter, at most one check every 2 s, each taking up
 *    to `CHECK_IDS` of the ids waiting, oldest first, whichever batch brought
 *    them. What comes back entered the list. Over `RELOAD_OVER` ids waiting,
 *    it reads every row its pages span instead.
 *
 * Rows that entered are kept aside as arrivals, never slipped into the rows on
 * screen, until `merge` joins them: nothing moves under the user (see
 * `LiveRows`). The cached pages stay as the server sent them, updated in place
 * only, so a full page still means Load more may find more; `rows` shows them
 * with the joined rows and without those merged away. A gap recovery reads
 * every row the pages span, from the newest down to the oldest loaded, so rows
 * that entered anywhere in them are found. A batch that comes while a page is
 * being read waits for it.
 */
export class ListLive {
  readonly #client: QueryClient;
  readonly #request: ListRequest;
  readonly #pageSize: number;
  readonly #now: () => number;
  #rows: RowsOnScreen = NOTHING_ON_SCREEN;
  #stale = new Set<string>();
  /** Past a page of arrivals only their ids are kept (null rows), since merging them reads the pages afresh. */
  #arrivals = new Map<string, InquiryRow | null>();
  #left = new Set<string>();
  #joined: ReadonlyMap<string, InquiryRow> = new Map();
  #gone: ReadonlySet<string> = new Set();
  /** Ids it does not hold, waiting for a membership check, oldest first. */
  #waiting = new Set<string>();
  /** Waiting ids a check by their ends could not settle: the next check names them whole. */
  #collided = new Set<string>();
  /** What came while a page was being read. */
  #early: Batch | null = null;
  #lastCheckAt = -Infinity;
  #failure: Failure | null = null;
  #snapshot: ListSnapshot = { arrivals: 0, left: new Set(), joined: new Map(), gone: new Set(), failure: null };
  readonly #listeners = new Set<() => void>();

  constructor(client: QueryClient, request: ListRequest, pageSize: number, now: () => number = Date.now) {
    this.#client = client;
    this.#request = request;
    this.#pageSize = pageSize;
    this.#now = now;
  }

  /** Say which rows are on screen; until then, none is. */
  watch(rows: RowsOnScreen): void {
    this.#rows = rows;
  }

  /** Whether `id` is a row that changed off screen and waits to come on screen. */
  isStale(id: string): boolean {
    return this.#stale.has(id);
  }

  /**
   * Apply one batch; resolves with `Later` while ids wait for a membership
   * check or a page. Stops before a read once `open` says the tab is hidden.
   */
  async update(batch: Batch, open: () => boolean = () => true): Promise<Later | null> {
    const pages = this.#pages();
    const { ids, gap } = joinBatches(this.#early, batch);
    // A page's read under way may predate these changes, so they wait for it.
    if (pages.some((page) => page.rows === undefined && page.fetching)) {
      this.#early = { ids, gap };
      return { afterMs: PAGE_WAIT_MS };
    }
    this.#early = null;
    // Nothing loaded, or only failed pages: their next read postdates these changes.
    if (!pages.some((page) => page.rows)) return null;
    const held = this.#held(pages);
    const changed = new Set<string>();
    for (const id of ids) (held.has(id) ? changed : this.#waiting).add(id);
    if (gap || this.#waiting.size > RELOAD_OVER) {
      await this.#reloadLoaded(changed, gap);
      if (!open()) return null;
    }
    await this.#refreshHeld(changed);
    if (!open()) return null;
    return this.#checkWaiting();
  }

  /**
   * Join the arrivals to the rows shown; with `dropLeft`, also take away the
   * rows that no longer match. The pill does both; an idle list at its top
   * only joins arrivals. Returns the rows it joined. More than a page of rows
   * merged in is not kept apart from the pages: the list reads them afresh
   * instead, and returns no rows.
   */
  merge(dropLeft: boolean): readonly InquiryRow[] {
    const arrived = [...this.#arrivals.values()];
    if (arrived.length === 0 && !(dropLeft && this.#left.size > 0)) return [];
    const pages = this.#pages();
    const loaded = heldRows(pages);
    // Merged rows a page now holds are shown from it, and only a page's rows need hiding.
    const joined = new Map([...this.#joined].filter(([id]) => !loaded.has(id)));
    const gone = new Set([...this.#gone].filter((id) => loaded.has(id)));
    const adding = arrived.filter((row) => !row || !loaded.has(row.id));
    if (adding.some((row) => !row) || joined.size + adding.length > this.#pageSize) {
      this.#reloadPages(dropLeft);
      return [];
    }
    const rows = arrived.filter((row) => row !== null);
    for (const row of rows) {
      gone.delete(row.id);
      if (!loaded.has(row.id)) joined.set(row.id, row);
    }
    // A row merged away that matched again is still on its page: freshen it there.
    void this.#freshen(new Map(rows.filter((row) => loaded.has(row.id)).map((row) => [row.id, row])));
    this.#arrivals.clear();
    if (dropLeft) {
      for (const id of this.#left) {
        joined.delete(id);
        if (loaded.has(id)) gone.add(id);
      }
      this.#left.clear();
    }
    this.#joined = joined;
    this.#gone = gone;
    this.#publish();
    return rows;
  }

  /**
   * The rows `loaded` from this list's pages as the list shows them: without
   * those merged away, with those merged in, each kind in the server's order.
   */
  rows(loaded: readonly InquiryRow[], { joined, gone }: ListSnapshot = this.#snapshot): readonly InquiryRow[] {
    if (joined.size === 0 && gone.size === 0) return loaded;
    const kept = loaded.filter((row) => !gone.has(row.id));
    const loadedIds = new Set(loaded.map((row) => row.id));
    const added = [...joined.values()].filter((row) => !loadedIds.has(row.id));
    if (added.length === 0) return kept;
    return this.#request.kinds.flatMap((kind) =>
      [...kept, ...added].filter((row) => row.kind === kind).sort(serverOrder),
    );
  }

  /**
   * The list shows `loaded`: an arrival among them, brought in by a page read
   * afresh (as after the user's own write), no longer waits behind the pill.
   */
  settle(loaded: readonly InquiryRow[]): void {
    if (this.#arrivals.size === 0) return;
    const shown = new Set(loaded.map((row) => row.id));
    for (const id of this.#arrivals.keys()) if (shown.has(id) && !this.#gone.has(id)) this.#arrivals.delete(id);
    this.#publish();
  }

  /** The stream layer's word on its updates: they keep failing, or, null, work again. */
  readonly failing = (failure: Failure | null): void => {
    this.#failure = failure;
    this.#publish();
  };

  /** The list is gone: forget what waits. Its pages stay as the server sent them. */
  release(): void {
    this.#stale.clear();
    this.#waiting.clear();
    this.#collided.clear();
    this.#early = null;
  }

  readonly subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  readonly getSnapshot = (): ListSnapshot => this.#snapshot;

  /** Whether `query` is one of this list's pages: its filters, a kind it lists, its page size. */
  #isPage(query: Query): boolean {
    const page = isPageOf(query, this.#request.filters) ? pageOf(query) : null;
    return page !== null && page.pageSize === this.#pageSize && this.#request.kinds.includes(page.kind);
  }

  /** This list's mounted pages in the cache, in offset order. */
  #pages(): Page[] {
    return this.#client
      .getQueryCache()
      .findAll({ predicate: (query) => this.#isPage(query) && query.getObserversCount() > 0 })
      .flatMap((query) => pageOf(query) ?? [])
      .sort((a, b) => a.offset - b.offset);
  }

  /** Every row the list holds, by id: its pages' rows not merged away, and the rows merged in. */
  #held(pages: readonly Page[]): Map<string, InquiryRow> {
    const held = heldRows(pages);
    for (const id of this.#gone) held.delete(id);
    for (const [id, row] of this.#joined) if (!held.has(id)) held.set(id, row);
    return held;
  }

  /**
   * Read every row the loaded pages span, from the newest down to the oldest
   * loaded one, in one request, and take it: rows it holds update in place, the
   * others arrive. When no kind came back full, the answer spans the pages
   * whole, so what it did not bring back is not in them: held rows it left out
   * leave, such arrivals go, and no id waits for a check. Otherwise the ids it
   * did not answer for wait, and after a gap, held rows it left out count as
   * `changed`.
   */
  async #reloadLoaded(changed: Set<string>, gap: boolean): Promise<void> {
    const { kinds, filters } = this.#request;
    const floor = oldestLoaded(this.#pages(), kinds);
    const spanned: Filter[] = floor === null ? [...filters] : [...filters, { field: "created", op: "ge", value: serverText(floor) }];
    const rows = await readOnce(this.#client, ["loaded", kinds, spanned], (signal) =>
      listInquiries({ kinds: inquiryKinds(kinds), filters: spanned, limit: MAX_ROWS, offset: 0, fields: INQUIRY_ROW_FIELDS }, { signal }),
    );
    const answered = new Set(rows.map((row) => row.id));
    if (kinds.every((kind) => rows.filter((row) => row.kind === kind).length < MAX_ROWS)) {
      await this.#take(rows, new Set([...answered, ...this.#held(this.#pages()).keys(), ...this.#arrivals.keys()]));
      changed.clear();
      this.#stale.clear();
      this.#waiting.clear();
      this.#collided.clear();
      return;
    }
    await this.#take(rows, answered);
    for (const id of answered) this.#waiting.delete(id);
    if (!gap) return;
    for (const id of this.#held(this.#pages()).keys()) if (!answered.has(id)) changed.add(id);
    for (const id of this.#arrivals.keys()) if (!answered.has(id)) this.#waiting.add(id);
  }

  /** Steps 1 and 2 for `changed` rows, and any stale row now on screen. */
  async #refreshHeld(changed: ReadonlySet<string>): Promise<void> {
    const held = this.#held(this.#pages());
    for (const id of this.#stale) if (!held.has(id)) this.#stale.delete(id);
    const candidates = [...new Set([...changed, ...this.#stale])].filter((id) => held.has(id));
    const shown = this.#rows.onScreen(candidates);
    for (const id of candidates) if (!shown.has(id)) this.#stale.add(id);
    const asked = candidates.filter((id) => shown.has(id)).map((id) => held.get(id)!);
    if (asked.length === 0) return;
    const { filters } = this.#request;
    const kinds = [...new Set(asked.map((row) => row.kind))];
    const seqs = new Set(asked.map((row) => row.seq));
    const ranges = seqRanges(seqs);
    // Every range applies to every kind, so each kind can match every seq asked.
    const rows = await readOnce(this.#client, ["seq", kinds, filters, ranges], (signal) =>
      listInquiriesBySeq({ kinds: inquiryKinds(kinds), filters, seqRanges: ranges, limit: seqs.size, fields: INQUIRY_ROW_FIELDS }, { signal }),
    );
    for (const row of asked) this.#stale.delete(row.id);
    await this.#take(rows, new Set(asked.map((row) => row.id)));
  }

  /**
   * Step 3: check up to `CHECK_IDS` waiting ids, oldest first, at most once
   * every 2 s. An id stops waiting only once a check has answered for it: a
   * full answer may have left an asked row out for others whose ids end the
   * same way, so the ids it did not bring back are checked again, whole.
   */
  async #checkWaiting(): Promise<Later | null> {
    if (this.#waiting.size === 0) return null;
    const wait = this.#lastCheckAt + CHECK_EVERY_MS - this.#now();
    if (wait > 0) return { afterMs: wait };
    const checked = this.#collided.size > 0 ? [...this.#collided].slice(0, WHOLE_IDS) : [...this.#waiting].slice(0, CHECK_IDS);
    this.#lastCheckAt = this.#now();
    const { kinds, filters } = this.#request;
    const withId = [...filters, idFilter(checked)];
    // Room for rows whose ids end like an asked one's (see `idFilter`).
    const limit = 2 * checked.length;
    const rows = await readOnce(this.#client, ["check", kinds, withId], (signal) =>
      listInquiries({ kinds: inquiryKinds(kinds), filters: withId, limit, offset: 0, fields: INQUIRY_ROW_FIELDS }, { signal }),
    );
    const back = new Set(rows.map((row) => row.id));
    for (const id of checked) {
      if (rows.length === limit && !back.has(id)) this.#collided.add(id);
      else {
        this.#collided.delete(id);
        this.#waiting.delete(id);
      }
    }
    await this.#take(rows, new Set(checked.filter((id) => !this.#collided.has(id))));
    return this.#waiting.size > 0 ? { afterMs: CHECK_EVERY_MS } : null;
  }

  /**
   * Take a fresh answer for the ids in `asked`: rows it holds update in place,
   * others arrive; asked ids that did not come back no longer match.
   */
  async #take(rows: readonly InquiryRow[], asked: ReadonlySet<string>): Promise<void> {
    const pages = this.#pages();
    const loaded = heldRows(pages);
    const held = this.#held(pages);
    const fresh = new Map<string, InquiryRow>();
    for (const row of rows) {
      if (held.has(row.id)) fresh.set(row.id, row);
      // A row merged away is still on its page, where it can come back.
      else if (asked.has(row.id) && (loaded.has(row.id) || placeRow(pages.filter((page) => page.kind === row.kind), row))) {
        this.#arrive(row);
      }
    }
    for (const id of fresh.keys()) this.#left.delete(id);
    const back = new Set(rows.map((row) => row.id));
    for (const id of asked) {
      if (back.has(id)) continue;
      if (held.has(id)) this.#left.add(id);
      else this.#arrivals.delete(id);
    }
    const joined = [...fresh].filter(([id]) => this.#joined.has(id));
    if (joined.length > 0) this.#joined = new Map([...this.#joined, ...joined]);
    this.#publish();
    await this.#freshen(fresh);
  }

  /** Count `row` as arrived, keeping it only while there is at most a page of arrivals. */
  #arrive(row: InquiryRow): void {
    const kept = this.#arrivals.get(row.id);
    const keep = kept === undefined ? this.#arrivals.size < this.#pageSize : kept !== null;
    this.#arrivals.set(row.id, keep ? row : null);
  }

  /**
   * Put the `fresh` rows in place on the pages that hold them. A read of such a
   * page under way may predate them, and would put the older rows back when it
   * answers: they go in after it, where it did not bring a later version.
   */
  async #freshen(fresh: ReadonlyMap<string, InquiryRow>): Promise<void> {
    if (fresh.size === 0) return;
    const holding = () => this.#pages().filter((page) => page.rows?.some((row) => fresh.has(row.id)));
    await Promise.all(
      holding()
        .filter((page) => page.fetching)
        .map((page) => this.#client.refetchQueries({ queryKey: page.key, exact: true }, { cancelRefetch: false })),
    );
    for (const page of holding()) {
      this.#client.setQueryData(page.key, page.rows!.map((row) => newerRow(fresh.get(row.id), row)));
    }
  }

  /**
   * Read the list's pages afresh, for more rows merged in than a page: they
   * show from the pages then, and nothing merged is kept apart. Rows that no
   * longer match stay dimmed until then, and after it unless `dropLeft`.
   */
  #reloadPages(dropLeft: boolean): void {
    this.#arrivals.clear();
    this.#publish();
    void this.#client.invalidateQueries({ predicate: (query) => this.#isPage(query) }).then(() => {
      this.#joined = new Map();
      this.#gone = new Set();
      if (dropLeft) this.#left.clear();
      this.#publish();
    });
  }

  #publish(): void {
    const { arrivals, left, joined, gone, failure } = this.#snapshot;
    const same =
      arrivals === this.#arrivals.size &&
      sameSet(left, this.#left) &&
      joined === this.#joined &&
      gone === this.#gone &&
      failure === this.#failure;
    if (same) return;
    this.#snapshot = {
      arrivals: this.#arrivals.size,
      left: new Set(this.#left),
      joined: this.#joined,
      gone: this.#gone,
      failure: this.#failure,
    };
    for (const listener of this.#listeners) listener();
  }
}

/** The least time between two membership checks of one list (the plan's request caps). */
const CHECK_EVERY_MS = 2_000;

/** How often a batch that came while a page was being read looks again (a page takes 0.3 to 0.6 s). */
const PAGE_WAIT_MS = 500;

/** The most rows a list request takes per kind (`MAX_LIST_LIMIT` in the server's `wire/routes.py`). */
const MAX_ROWS = 1_000;

const NOTHING_ON_SCREEN: RowsOnScreen = { onScreen: () => new Set() };

/** `later` and whatever came `earlier` as one batch. */
function joinBatches(earlier: Batch | null, later: Batch): Batch {
  return earlier ? { ids: new Set([...earlier.ids, ...later.ids]), gap: earlier.gap || later.gap } : later;
}

/**
 * Where the loaded rows end: the `created` of the oldest last row among the
 * kinds whose last loaded page came back full. Null when a kind has loaded
 * every row it has, so a read spanning the pages must take every row.
 */
function oldestLoaded(pages: readonly Page[], kinds: readonly string[]): string | null {
  let oldest: InquiryRow | null = null;
  for (const kind of kinds) {
    const last = pages.filter((page) => page.kind === kind && page.rows).at(-1);
    // A kind with nothing loaded takes no row from the answer (see `placeRow`).
    if (!last?.rows) continue;
    const row = last.rows.at(-1);
    if (!row || last.rows.length < last.pageSize) return null;
    if (!oldest || serverOrder(row, oldest) > 0) oldest = row;
  }
  return oldest?.created ?? null;
}

function sameSet(a: ReadonlySet<string>, b: ReadonlySet<string>): boolean {
  return a.size === b.size && [...a].every((id) => b.has(id));
}
