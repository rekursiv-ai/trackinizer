import { queryOptions, skipToken, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { type ChangeKind, type LoggedChange, listChanges } from "../api/changes";
import { inquiryKinds, listInquiries } from "../api/inquiries";
import type { Mention } from "./feed";

/** Changes per page: the 50 rows measured at 0.15 s a query. */
const PAGE_SIZE = 50;

const NO_CHANGES: readonly LoggedChange[] = [];

/**
 * How many ids one lookup names. A filter value is capped at 512 characters
 * (`MAX_FILTER_VALUE_CHARS`), and `^(id|…|id)$` over 13 UUIDs is 484.
 */
const LOOKUP_IDS = 13;

/** The Activity view's reads, as TanStack Query options, so the stream layer can find them by key. */
export const activityQueries = {
  /** One page of a tab's change kinds, in one request: the newest, or those older than `afterId`. */
  page: (kinds: readonly ChangeKind[], afterId: string | null) =>
    queryOptions({
      queryKey: ["activity", "changes", kinds, afterId],
      queryFn: ({ signal }) => listChanges({ kind: kinds, afterId: afterId ?? undefined, limit: PAGE_SIZE, brief: true }, { signal }),
    }),
  /**
   * A tab's changes newer than its first page, newest first. The live stream
   * layer writes them here (`src/live/activity.ts`); nothing fetches them.
   */
  head: (kinds: readonly ChangeKind[]) =>
    queryOptions({
      queryKey: ["activity", "head", kinds],
      queryFn: skipToken,
      initialData: NO_CHANGES,
      staleTime: Infinity,
    }),
  /**
   * The id, kind, seq and title of up to `LOOKUP_IDS` inquiries; a purged one is
   * absent. A list of rows, each with its `id`, so a write that touches one
   * refetches it as it does any cached row (`refetchShowing`); a title changes,
   * so each return to Activity reads them afresh too.
   */
  subjects: (mentions: readonly Mention[]) =>
    queryOptions({
      queryKey: ["activity", "subjects", mentions.map((m) => m.id)],
      queryFn: async ({ signal }): Promise<Subject[]> => {
        const rows = await listInquiries(
          {
            kinds: inquiryKinds([...new Set(mentions.map((m) => m.kind))]),
            filters: [{ field: "id", op: "re", value: `^(${mentions.map((m) => m.id).join("|")})$` }],
            limit: mentions.length,
            offset: 0,
            fields: ["id", "kind", "seq", "title"],
          },
          { signal },
        );
        return rows.map(({ id, kind, seq, title }) => ({ id, kind, seq, title }));
      },
    }),
};

/** What the feed has loaded of a tab's change kinds. */
export type Loaded = {
  /** The changes, newest first: the live head, then the pages. */
  readonly rows: readonly LoggedChange[];
  /** The last page came back full, so older changes may follow. */
  readonly more: boolean;
  /** The first page has answered, with rows or a failure. */
  readonly ready: boolean;
  /** Some page is on its way. */
  readonly loading: boolean;
  /** The first failure, if any page failed. */
  readonly error: Error | null;
  readonly retry: () => void;
};

/**
 * Load `pages` pages (at least one) of `kinds`, each page one request.
 *
 * Each page after the first starts after the last change of the one before,
 * read from the cache, so a page is fetched only once the one before it has
 * answered, and a view opened again shows its cached pages at once. A short
 * page is the only sign of the end. The log only grows at its new end, so
 * paging by cursor never repeats a change.
 */
export function useChangePages(kinds: readonly ChangeKind[], pages: number): Loaded {
  const queryClient = useQueryClient();
  const slots: (string | null)[] = [];
  let afterId: string | null = null;
  for (let page = 0; page < Math.max(1, pages); page++) {
    slots.push(afterId);
    const rows: LoggedChange[] | undefined = queryClient.getQueryData(activityQueries.page(kinds, afterId).queryKey);
    if (!rows || rows.length < PAGE_SIZE) break;
    afterId = rows.at(-1)!.id;
  }
  const results = useQueries({ queries: slots.map((after) => activityQueries.page(kinds, after)) });
  const head = useQuery(activityQueries.head(kinds));
  const loaded = results.flatMap((result) => (result.data ? [result.data] : []));
  const [first] = results;
  // A first page refetched by anything but the live layer (a write, a gap) can
  // hold changes the head still holds: each shows once.
  const rows = [...head.data!, ...loaded.flat()];
  const ids = rows.map((row) => row.id);
  return {
    rows: rows.filter((row, index) => ids.indexOf(row.id) === index),
    more: loaded.at(-1)?.length === PAGE_SIZE,
    ready: first !== undefined && (first.data !== undefined || first.isError),
    loading: results.some((result) => result.isFetching),
    error: results.find((result) => result.error)?.error ?? null,
    retry: () => results.forEach((result) => result.error && void result.refetch()),
  };
}

/** What a mention resolved to; absent while it loads, and for a purged inquiry. */
export type Subject = { readonly id: string; readonly kind: string; readonly seq: number; readonly title: string };

/** The subjects looked up so far, by id, and the first lookup that failed. */
export type Subjects = {
  readonly byId: ReadonlyMap<string, Subject>;
  readonly error: Error | null;
  /** Ask again for every lookup that failed. */
  readonly retry: () => void;
};

/**
 * Look up every inquiry `mentions` names, `LOOKUP_IDS` at a time.
 *
 * The change log names inquiries only by id and kind, so a line's `Kind#seq`
 * and title come from `GET /api/inquiries` with an `id` regex, as the live
 * layer's membership check asks. Lines show at once and fill in as lookups
 * answer; one that fails says so, since its lines would otherwise stay bare.
 */
export function useSubjects(mentions: readonly Mention[]): Subjects {
  const chunks = Array.from({ length: Math.ceil(mentions.length / LOOKUP_IDS) }, (_, k) =>
    mentions.slice(k * LOOKUP_IDS, (k + 1) * LOOKUP_IDS),
  );
  return useQueries({
    queries: chunks.map((chunk) => activityQueries.subjects(chunk)),
    combine: (results) => ({
      byId: new Map(results.flatMap((result) => (result.data ?? []).map((subject) => [subject.id, subject] as const))),
      error: results.find((result) => result.error && !result.isFetching)?.error ?? null,
      retry: () => results.forEach((result) => result.error && void result.refetch()),
    }),
  });
}
