import { keepPreviousData, useQueries, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { INQUIRY_ROW_FIELDS, type InquiryRow, inquiryKinds, listInquiries } from "../api/inquiries";
import { prefetched } from "../app/prefetch";
import type { Filter, ListRequest } from "../query/query";

/** What a list has loaded so far. */
export type LoadedRows = {
  /** Every loaded row once, kinds in the request's order, each kind newest created first. */
  readonly rows: readonly InquiryRow[];
  /** Kinds whose last page came back full, so Load more may find more. */
  readonly more: readonly string[];
  /** Kinds with a page on its way. */
  readonly loading: readonly string[];
  /** Nothing to show yet: no rows, and a page still on its way. */
  readonly pending: boolean;
  /** The first failure, if any page failed. */
  readonly error: Error | null;
  readonly retry: () => void;
};

/**
 * Where a page after the first starts: after the last row loaded before it.
 *
 * The server lists newest created first, then the larger id. So the next page is
 * the rows created at or before `created` (as the server compares it, as text),
 * less `ties`, the rows already loaded at that very time.
 */
export type After = { readonly created: string; readonly ties: readonly string[] };

/**
 * Load `pages[kind]` pages (at least one) of `pageSize` rows for each kind.
 *
 * One query per kind and page, keyed by the request, kind, place and where the
 * page starts, so Load more fetches only its new page and a list opened again
 * shows its cached pages at once. There is no total: a full last page is the only
 * sign of more. While a changed filter loads, the old rows stay on screen. The
 * page a first view shows may already be on its way (`prefetch`).
 *
 * A page starts after the last row loaded, not at a count of rows: a row that
 * left the list ahead of an offset would move every later row up one, and the
 * next page would skip the first of them. So each page after the first waits
 * for the one before it, as Activity's pages do.
 */
export function useListPages(
  request: ListRequest,
  pageSize: number,
  pages: { readonly [kind: string]: number },
): LoadedRows {
  const queryClient = useQueryClient();
  const slots = request.kinds.flatMap((kind) => {
    const out: Slot[] = [];
    const loaded: InquiryRow[] = [];
    for (let page = 0; page < Math.max(1, pages[kind] ?? 1); page++) {
      const after = page === 0 ? null : afterRows(loaded);
      const slot = { kind, offset: page * pageSize, after };
      out.push(slot);
      const rows = queryClient.getQueryData<InquiryRow[]>(pageKey(request.filters, pageSize, slot));
      if (!rows || rows.length < pageSize) break;
      loaded.push(...rows);
    }
    return out;
  });
  const results = useQueries({
    queries: slots.map((slot) => ({
      queryKey: pageKey(request.filters, pageSize, slot),
      queryFn: ({ queryKey, signal }: { queryKey: readonly unknown[]; signal: AbortSignal }) =>
        prefetched(queryKey, () => readPage(request.filters, pageSize, slot, signal)),
      placeholderData: keepPreviousData,
    })),
  });
  return combine(slots, results, pageSize);
}

/** One page to load: its kind, its place (`offset` rows in), and where it starts. */
type Slot = { readonly kind: string; readonly offset: number; readonly after: After | null };

/**
 * A page's key. The live layer reads the kind, page size and place from it
 * (`pageOf` in `src/live/rows.ts`), and matches a list by its filters.
 */
function pageKey(filters: readonly Filter[], pageSize: number, { kind, offset, after }: Slot) {
  return ["inquiries", "list", filters, kind, pageSize, offset, after] as const;
}

async function readPage(
  filters: readonly Filter[],
  pageSize: number,
  { kind, after }: Slot,
  signal: AbortSignal,
): Promise<InquiryRow[]> {
  const kinds = inquiryKinds([kind]);
  if (after === null) return listInquiries({ kinds, filters, limit: pageSize, offset: 0, fields: INQUIRY_ROW_FIELDS }, { signal });
  const rows = await listInquiries(
    {
      kinds,
      filters: [...filters, { field: "created", op: "le", value: after.created }],
      // Room for the ties, which come back too, so a full page is still full.
      limit: pageSize + after.ties.length,
      offset: 0,
      fields: INQUIRY_ROW_FIELDS,
    },
    { signal },
  );
  return rows.filter((row) => !after.ties.includes(row.id)).slice(0, pageSize);
}

/** Where the page after `loaded` starts: after its last row, in the server's order. */
function afterRows(loaded: readonly InquiryRow[]): After | null {
  const last = loaded.at(-1);
  if (!last) return null;
  return {
    created: serverText(last.created),
    ties: loaded.filter((row) => row.created === last.created).map((row) => row.id),
  };
}

/**
 * A time as the server compares one in a filter: text, in UTC, as Python's
 * `str(datetime)` writes it (`_TS_TEXT` in `wire/column_shapes.py`): a space
 * before the time, six digits of fraction or none, then `+00:00`.
 */
export function serverText(iso: string): string {
  const fraction = (/\.(\d+)/.exec(iso)?.[1] ?? "").slice(0, 6).padEnd(6, "0");
  const whole = new Date(Date.parse(iso.replace(/\.\d+/, ""))).toISOString().slice(0, 19).replace("T", " ");
  return `${whole}${/^0+$/.test(fraction) ? "" : `.${fraction}`}+00:00`;
}

function combine(
  slots: readonly Slot[],
  results: readonly UseQueryResult<InquiryRow[]>[],
  pageSize: number,
): LoadedRows {
  const seen = new Set<string>();
  const rows: InquiryRow[] = [];
  const last = new Map<string, InquiryRow[] | undefined>();
  const loading = new Set<string>();
  results.forEach((result, index) => {
    const { kind } = slots[index]!;
    // A page refetched on its own can overlap the next one, which starts after
    // the rows it had then; each row shows once.
    for (const row of result.data ?? []) {
      if (!seen.has(row.id)) rows.push(row);
      seen.add(row.id);
    }
    last.set(kind, result.data);
    if (result.isFetching) loading.add(kind);
  });
  const failed = results.find((result) => result.error);
  return {
    rows,
    more: [...last].filter(([, page]) => page?.length === pageSize).map(([kind]) => kind),
    loading: [...loading],
    pending: rows.length === 0 && results.some((result) => result.isPending),
    error: failed?.error ?? null,
    retry: () => results.forEach((result) => result.error && void result.refetch()),
  };
}
