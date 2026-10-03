import { type QueryClient, useQueries } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import type { Detail, DetailRow } from "../api/detail";
import type { InquiryRow } from "../api/inquiries";
import { searchInquiries } from "../api/search";
import { openedAt } from "../detail/queries";

/** An inquiry the palette lists: what its row shows, and what a search reads. */
export type Found = {
  readonly id: string;
  readonly kind: string;
  readonly seq: number;
  readonly title: string;
  readonly status: string;
  readonly judgement?: string | null;
  readonly owner?: string | null;
  /** Absent on a relation's far end, whose description the palette never loaded. */
  readonly description?: string | null;
  /** Absent on a relation's far end. */
  readonly modified?: string;
};

/**
 * Every inquiry the app has loaded, once each, most recently modified first:
 * list pages, and open and recent details with the far ends of their relations.
 * The far ends, which carry no modified time, come last. Search results are not
 * among them: they are the server's rows, listed apart, and counting them here
 * would move each one up into the loaded rows as it arrived.
 */
export function cachedInquiries(queryClient: QueryClient): Found[] {
  const byId = new Map<string, Found>();
  const add = (found: Found) => {
    const held = byId.get(found.id);
    if (!held || modifiedAt(found) > modifiedAt(held)) byId.set(found.id, found);
  };
  for (const [, page] of queryClient.getQueriesData<InquiryRow[]>({ queryKey: ["inquiries", "list"] })) {
    page?.forEach(add);
  }
  for (const [, detail] of queryClient.getQueriesData<Detail>({ queryKey: ["detail"] })) {
    if (!detail) continue;
    add(foundOf(detail.self));
    for (const peers of [...Object.values(detail.edges), ...Object.values(detail.backlinks)]) {
      peers.forEach(add);
    }
  }
  return [...byId.values()].sort((a, b) => modifiedAt(b) - modifiedAt(a));
}

/** The inquiries whose details were opened, still cached, the last opened first. */
export function recentDetails(queryClient: QueryClient): Found[] {
  return queryClient
    .getQueriesData<Detail>({ queryKey: ["detail"] })
    .flatMap(([, detail]) => {
      const at = detail ? openedAt(queryClient, detail.self.id) : 0;
      return detail && at > 0 ? [{ found: foundOf(detail.self), at }] : [];
    })
    .sort((a, b) => b.at - a.at)
    .map(({ found }) => found);
}

/**
 * `cachedInquiries` and `recentDetails`, read again whenever a list page or a
 * detail gets new data or leaves the cache, so an open palette lists what loads
 * under it. Other cache events, such as a fetch starting, change neither.
 */
export function useLoaded(queryClient: QueryClient): { cached: Found[]; recent: Found[] } {
  const version = useRef(0);
  const subscribe = useCallback(
    (changed: () => void) =>
      queryClient.getQueryCache().subscribe((event) => {
        const [scope, name] = event.query.queryKey;
        const rows = scope === "detail" || (scope === "inquiries" && name === "list");
        const landed = (event.type === "updated" && event.action.type === "success") || event.type === "removed";
        if (!rows || !landed) return;
        version.current += 1;
        changed();
      }),
    [queryClient],
  );
  const at = useSyncExternalStore(subscribe, () => version.current);
  return useMemo(
    () => ({ cached: cachedInquiries(queryClient), recent: recentDetails(queryClient) }),
    // `at` is not read inside: it is what changes when their answer would.
    [queryClient, at],
  );
}

/** One kind's server search. */
export type KindSearch = {
  readonly kind: string;
  /** `undefined` until the first answer. */
  readonly hits: readonly Found[] | undefined;
  /** The last failure, unless a new attempt is under way. */
  readonly error: Error | null;
  /** No answer yet, or a new attempt under way. */
  readonly pending: boolean;
  /** When the hits came back, in ms since the epoch; 0 before. */
  readonly updatedAt: number;
  readonly retry: () => void;
};

/** How long typing must pause before the palette asks the server. */
export const SEARCH_DEBOUNCE_MS = 300;

/**
 * Search the server for `text`: one request per kind, Issues first, `limit` 5,
 * once typing has paused for 300 ms, or at once through `searchNow`. `waiting`
 * says a search is due once the pause ends.
 *
 * A search costs up to 1.5 s per kind, so none runs per keystroke. A keystroke
 * drops every request for the older text at once, and the cache cancels each
 * one still in flight. The server keeps running a cancelled search to its 5 s
 * budget regardless, which is why the pause matters more than the cancel.
 * Results are a snapshot, not refetched on their own, and kept by text and kind,
 * so text typed again shows its earlier answer, with that answer's time. `immediate`
 * searches the starting text without the pause, as a `#/search/<q>` link does.
 */
export function useServerSearch(
  text: string,
  kinds: readonly string[],
  { immediate }: { immediate: boolean },
): { searches: KindSearch[]; waiting: boolean; searchNow: () => void } {
  const q = text.trim();
  const [searched, setSearched] = useState(immediate ? q : "");
  useEffect(() => {
    if (q === searched) return;
    const timer = setTimeout(() => setSearched(q), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [q, searched]);
  const active = q && q === searched ? q : null;
  const ordered = [...kinds].sort((a, b) => Number(b === "Issue") - Number(a === "Issue"));
  const results = useQueries({
    queries: active
      ? ordered.map((kind) => ({
          queryKey: ["search", active, kind],
          queryFn: ({ signal }: { signal: AbortSignal }) => searchInquiries({ q: active, kind, limit: 5 }, { signal }),
          staleTime: Infinity,
        }))
      : [],
  });
  return {
    waiting: q !== "" && q !== searched,
    searches: results.map((result, index) => ({
      kind: ordered[index]!,
      hits: result.data?.map(foundOf),
      error: result.isFetching ? null : result.error,
      pending: result.isPending || result.isFetching,
      updatedAt: result.dataUpdatedAt,
      retry: () => void result.refetch(),
    })),
    searchNow: () => setSearched(q),
  };
}

/** A row as `/api/web/get`'s `self` or a search hit, down to what the palette uses. */
function foundOf(row: DetailRow): Found {
  return {
    id: row.id,
    kind: row.kind,
    seq: row.seq,
    title: row.title,
    status: row.status,
    judgement: text(row.judgement),
    owner: text(row.owner),
    description: text(row.description),
    modified: row.modified,
  };
}

function text(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function modifiedAt(found: Found): number {
  return found.modified ? Date.parse(found.modified) : -Infinity;
}
