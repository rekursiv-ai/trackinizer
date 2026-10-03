import { hashKey, type Query, type QueryClient } from "@tanstack/react-query";
import type { Detail } from "../api/detail";
import { detailQueries } from "../detail/queries";
import { refetchFresh } from "./cache";
import type { Batch, Later } from "./serial";

/**
 * An open detail, kept current by the stream (step 4 in the plan's "Live
 * updates").
 *
 * When the batch has the focus, every read keyed by its id refetches: the
 * detail, evidence confidence, metrics, a transcript's parts listing (`keyedBy`).
 * When it has a neighbour the detail shows, the detail refetches with its
 * evidence confidence, which the neighbours' evidence derives: an edge change
 * writes a change on both ends, so relations added or removed arrive this way.
 * Metrics and transcripts are no neighbour's. A gap recovery refetches
 * everything keyed by the id.
 */
export class DetailLive {
  readonly #client: QueryClient;
  readonly #id: string;

  constructor(client: QueryClient, id: string) {
    this.#client = client;
    this.#id = id;
  }

  async update({ ids, gap }: Batch): Promise<Later | null> {
    const id = this.#id;
    if (gap || ids.has(id)) {
      await refetchFresh(this.#client, { type: "active", predicate: (query) => keyedBy(query, id) });
    } else if (this.#showsNeighbour(ids)) {
      const keys = [detailQueries.detail(id).queryKey, detailQueries.confidence(id).queryKey].map((key) => hashKey(key));
      await refetchFresh(this.#client, { type: "active", predicate: (query) => keys.includes(query.queryHash) });
    }
    return null;
  }

  /** Whether `ids` has a neighbour the detail shows, or may: its first read, under way, may predate them. */
  #showsNeighbour(ids: ReadonlySet<string>): boolean {
    const key = detailQueries.detail(this.#id).queryKey;
    const detail = this.#client.getQueryData<Detail>(key);
    if (!detail) return ids.size > 0 && this.#client.getQueryState(key)?.fetchStatus === "fetching";
    return [detail.edges, detail.backlinks].some((byEdge) =>
      Object.values(byEdge).some((peers) => peers.some((peer) => ids.has(peer.id))),
    );
  }
}

/**
 * Whether `query` reads something of the inquiry `id`: its key names the id.
 *
 * Search results never refetch: they are a snapshot of when the search ran, and
 * the query text in their key could be that very id. Nor do a transcript's
 * records: each part reads what was appended once its parts listing, keyed by
 * the id too, shows more (`Part` in `detail/transcript`), and a refetch would
 * read every record it holds again, page by page.
 */
function keyedBy(query: Query, id: string): boolean {
  const [head, , read] = query.queryKey;
  return head !== "search" && !(head === "session" && read === "records") && query.queryKey.includes(id);
}
