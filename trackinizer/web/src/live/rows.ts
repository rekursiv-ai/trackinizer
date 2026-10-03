// Pure helpers over the rows a live list holds: its cached pages, the seq ranges
// and id filters its requests carry, and where a row that entered it belongs.
import { hashKey, type Query, type QueryKey } from "@tanstack/react-query";
import type { InquiryRow } from "../api/inquiries";
import type { Filter } from "../query/query";

/**
 * The most ids one membership check can carry. A filter value is capped at 512
 * characters (`MAX_FILTER_VALUE_CHARS` in `wire/filters.py`). A check of this
 * many names each id by its last seven hex digits, so 63 make a 506-character
 * pattern and 64 make 514, which the server refuses. Whole UUIDs fit only 13,
 * and a check of 13 every 2 s fell behind a burst of changes to other rows.
 */
export const CHECK_IDS = 63;

/** The most ids a check can name whole: 13 UUIDs make a 483-character pattern. */
export const WHOLE_IDS = 13;

/** Over this many ids a list does not hold, it reads every row its pages span instead of checking them. */
export const RELOAD_OVER = 100;

/** One cached page of a list, as `useListPages` keys it. */
export type Page = {
  readonly key: QueryKey;
  readonly kind: string;
  readonly pageSize: number;
  readonly offset: number;
  /** Undefined until the page has loaded. */
  readonly rows: readonly InquiryRow[] | undefined;
  /** A read of the page is under way (or waits for the network). */
  readonly fetching: boolean;
};

/**
 * The page a list query holds, or null for a query of any other shape.
 *
 * The key is `["inquiries", "list", filters, kind, pageSize, offset]`, as
 * `useListPages` (`src/lists/pages.ts`) builds it.
 */
export function pageOf(query: Query): Page | null {
  const [, , , kind, pageSize, offset] = query.queryKey;
  if (typeof kind !== "string" || typeof pageSize !== "number" || typeof offset !== "number") return null;
  return {
    key: query.queryKey,
    kind,
    pageSize,
    offset,
    rows: query.state.data as InquiryRow[] | undefined,
    fetching: query.state.fetchStatus !== "idle",
  };
}

/** Whether `query` is a page of the list with exactly these `filters`. */
export function isPageOf(query: Query, filters: readonly Filter[]): boolean {
  return isPageKey(query.queryKey, filters);
}

/** Whether `key` is a page's key, for the list with exactly these `filters`. */
export function isPageKey(key: readonly unknown[], filters: readonly Filter[]): boolean {
  // Compared by hash, not by TanStack's key prefix: a prefix match treats the
  // filter array as partial, so a list with one more filter would match too.
  const [scope, name, pageFilters] = key;
  return scope === "inquiries" && name === "list" && hashKey([pageFilters]) === hashKey([filters]);
}

/** Every row the pages hold, by id. */
export function heldRows(pages: readonly Page[]): Map<string, InquiryRow> {
  const held = new Map<string, InquiryRow>();
  for (const page of pages) for (const row of page.rows ?? []) held.set(row.id, row);
  return held;
}

/** `seqs` as the fewest inclusive `a..b` ranges: `[9, 3, 4, 5]` is `3..5` and `9..9`. */
export function seqRanges(seqs: Iterable<number>): string[] {
  const sorted = [...new Set(seqs)].sort((a, b) => a - b);
  const ranges: string[] = [];
  let start = sorted[0];
  for (let index = 1; index <= sorted.length; index++) {
    if (sorted[index] === sorted[index - 1]! + 1) continue;
    ranges.push(`${start}..${sorted[index - 1]}`);
    start = sorted[index];
  }
  return ranges;
}

/**
 * The filter a membership check adds: the row's id ends as one of `ids` does,
 * at most `CHECK_IDS` of them, each named by as long an end as fits in the
 * server's 512 characters: whole, up to `WHOLE_IDS` of them, and down to the
 * last seven hex digits for `CHECK_IDS`.
 *
 * Ids are random (UUID version 4), so another row's id ends in the same seven
 * digits about once per 268 million rows. Such a row comes back too; the caller
 * takes only the ids it asked for, and names whole any id such rows may have
 * crowded out of an answer.
 */
export function idFilter(ids: readonly string[]): Filter {
  if (ids.length > CHECK_IDS) throw new Error(`A membership check holds at most ${CHECK_IDS} ids, not ${ids.length}.`);
  // `(a|b|…)$` over n ends of w characters is n(w + 1) + 2 characters long.
  const width = Math.min(UUID_CHARS, Math.floor((MAX_FILTER_CHARS - 2) / ids.length) - 1);
  return { field: "id", op: "re", value: `(${ids.map((id) => id.slice(-width)).join("|")})$` };
}

/**
 * The server's list order: newest created first, then the larger id
 * (`ORDER BY created DESC, id DESC`), to the microsecond the server keeps.
 */
export function serverOrder(a: InquiryRow, b: InquiryRow): number {
  return micros(b.created) - micros(a.created) || (a.id < b.id ? 1 : a.id > b.id ? -1 : 0);
}

/** `fresh`, unless `held` is a later version of the row (by `modified`) or there is no `fresh`. */
export function newerRow(fresh: InquiryRow | undefined, held: InquiryRow): InquiryRow {
  return fresh && micros(fresh.modified) >= micros(held.modified) ? fresh : held;
}

/**
 * Where `row` goes among one kind's pages (in offset order), in the server's
 * order: the page and the index to insert it at.
 *
 * Null when it sorts after every loaded row while the server has more: it lies
 * beyond what the list loaded, and Load more will find it there. Null too while
 * the page it would join has not loaded.
 */
export function placeRow<P extends Page>(pages: readonly P[], row: InquiryRow): { page: P; index: number } | null {
  for (const page of pages) {
    if (!page.rows) return null;
    const index = page.rows.findIndex((other) => serverOrder(row, other) < 0);
    if (index >= 0) return { page, index };
  }
  const last = pages.at(-1);
  return last?.rows && last.rows.length < last.pageSize ? { page: last, index: last.rows.length } : null;
}

/**
 * Microseconds since the epoch. `Date.parse` keeps only milliseconds, and the
 * server prints no fraction when the microseconds are zero.
 */
export function micros(iso: string): number {
  const fraction = /\.(\d+)/.exec(iso)?.[1] ?? "";
  return Date.parse(iso.replace(/\.\d+/, "")) * 1_000 + Number(fraction.slice(0, 6).padEnd(6, "0"));
}

/** `MAX_FILTER_VALUE_CHARS` in `wire/filters.py`. */
const MAX_FILTER_CHARS = 512;

const UUID_CHARS = 36;
