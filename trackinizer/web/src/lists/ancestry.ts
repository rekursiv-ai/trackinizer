import { keepPreviousData, type QueryClient, useQuery, useQueryClient } from "@tanstack/react-query";
import { useContext, useEffect, useMemo } from "react";
import { type Ancestor, inquiryKinds, type ListedRow, listInquiries } from "../api/inquiries";
import { LiveContext } from "../live";
import { refetchFresh } from "../live/cache";
import { CHECK_IDS, idFilter } from "../live/rows";
import type { Batch, Later } from "../live/serial";

/** Each row's `narrows` ancestors, nearest first, by row id. */
export type Ancestry = ReadonlyMap<string, readonly Ancestor[]>;

/** What an ancestry read holds: each row asked for, by id, with its ancestors. */
type AncestryRows = readonly ListedRow<"id">[];

/** The ancestry read of the Issues `ids`; the order of the ids does not matter. */
export function ancestryKey(ids: readonly string[]) {
  return ["ancestry", ids.toSorted()] as const;
}

/**
 * The `narrows` ancestry of the Issues `ids`, the rows a list shows, kept
 * current while mounted; null reads nothing.
 *
 * It is read apart from the list's pages, by id, so a view that nests rows
 * under their parents shows the same pages, kept current by the same live
 * rules, as the flat list: switching views reads only this. While the ids
 * change (Load more, new rows merged in), the ancestry read before stays, and
 * the new rows have none until theirs arrives.
 */
export function useAncestry(ids: readonly string[] | null): {
  ancestry: Ancestry;
  pending: boolean;
  error: Error | null;
  retry: () => void;
} {
  const client = useQueryClient();
  const hub = useContext(LiveContext);
  const asked = ids !== null && ids.length > 0;
  const query = useQuery({
    queryKey: ancestryKey(ids ?? []),
    queryFn: ({ queryKey: [, sorted], signal }) => readAncestry(sorted, signal),
    enabled: asked,
    placeholderData: keepPreviousData,
  });
  useEffect(() => (hub && asked ? hub.register(new AncestryLive(client)).dispose : undefined), [hub, client, asked]);
  const ancestry = useMemo(() => new Map((query.data ?? []).map((row) => [row.id, row.ancestors ?? []])), [query.data]);
  return { ancestry, pending: asked && query.isPending, error: query.error, retry: () => void query.refetch() };
}

/**
 * Keeps the ancestry reads on screen current: one refetches when a batch names
 * a row it holds or one of their ancestors. A `narrows` edge added or removed
 * writes a change on both ends, so it comes this way, as does a new title or
 * status of an ancestor shown. A gap refetches every one.
 */
export class AncestryLive {
  readonly #client: QueryClient;

  constructor(client: QueryClient) {
    this.#client = client;
  }

  async update({ ids, gap }: Batch): Promise<Later | null> {
    await refetchFresh(this.#client, {
      queryKey: ["ancestry"],
      type: "active",
      predicate: ({ state }) =>
        gap ||
        // A first read under way may predate the batch.
        (state.data === undefined ? state.fetchStatus === "fetching" && ids.size > 0 : shows(state.data as AncestryRows, ids)),
    });
    return null;
  }
}

/**
 * Read the ancestry of `ids`, `CHECK_IDS` a request: an `id` filter of more
 * would pass the server's 512 characters. Each names its ids by their ends,
 * so a row whose id ends alike can come back too, and only the asked are kept.
 */
async function readAncestry(ids: readonly string[], signal: AbortSignal): Promise<AncestryRows> {
  const parts = Array.from({ length: Math.ceil(ids.length / CHECK_IDS) }, (_, n) => ids.slice(n * CHECK_IDS, (n + 1) * CHECK_IDS));
  const asked = new Set(ids);
  const answers = await Promise.all(
    parts.map((part) =>
      listInquiries(
        {
          kinds: inquiryKinds(["Issue"]),
          filters: [idFilter(part)],
          limit: 2 * part.length,
          offset: 0,
          fields: ["id"],
          ancestors: "narrows",
        },
        { signal },
      ),
    ),
  );
  return answers.flat().filter((row) => asked.has(row.id));
}

/** Whether `rows` hold one of `ids`, as a row or an ancestor. */
function shows(rows: AncestryRows, ids: ReadonlySet<string>): boolean {
  return rows.some((row) => ids.has(row.id) || (row.ancestors ?? []).some((up) => ids.has(up.id)));
}
