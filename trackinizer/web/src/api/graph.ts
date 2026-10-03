import { type CallOptions, client, send, TIMEOUT_MS } from "./client";

/** One inquiry in the graph: `_graph_node`'s light projection in `server/web.py`. */
export type GraphNode = {
  readonly id: string;
  readonly kind: string;
  readonly seq: number;
  readonly title: string;
  readonly status: string;
  readonly created: string;
  /** A Belief's verdict and certainty, absent when unset and on other kinds. */
  readonly judgement?: string;
  readonly confidence?: number;
};

/** One edge, stored child to parent: `from_id` narrows, or proves, `to_id`. */
export type GraphEdge = {
  readonly from_id: string;
  readonly to_id: string;
  readonly edge_kind: string;
  /** A citation's sign and weight, -1 to 1; absent on every other edge. */
  readonly valence?: number;
};

export type Graph = { readonly nodes: readonly GraphNode[]; readonly edges: readonly GraphEdge[] };

/**
 * The limit that asks for every inquiry: the largest an SQL integer holds, which
 * no graph reaches. The server takes any limit from 1 up.
 */
export const ALL_NODES = 2_147_483_647;

/**
 * Fetch `GET /api/web/graph`: at most `limit` inquiries, the newest each
 * followed by the older ones it links to, and only the edges between them.
 *
 * Written by hand from `web_graph` in `server/web.py`, which the schema types as
 * free JSON.
 */
export async function getGraph(limit: number, { signal }: CallOptions = {}): Promise<Graph> {
  const graph = await send(TIMEOUT_MS.read, signal, (signal) =>
    client.GET("/api/web/graph", { params: { query: { limit } }, signal }),
  );
  return graph as Graph;
}

/** One inquiry in a focus's neighbourhood: how many edges it lies from the focus, 0 for the focus. */
export type FocusNode = GraphNode & { readonly hops: number };

/** A focus's neighbourhood: the inquiries nearest it, and the edges between them. */
export type FocusGraph = { readonly nodes: readonly FocusNode[]; readonly edges: readonly GraphEdge[] };

/**
 * Fetch `GET /api/web/graph?focus=`: the focus and the inquiries nearest it, over
 * edges in either direction. All of one hop come before the next, newest first
 * within a hop, up to `hops` (the server's default is 2) and `limit` inquiries,
 * the focus included (default 60). Only the edges between them come back. An
 * unknown focus fails with 404.
 *
 * Written by hand from `web_graph` in `server/web.py`, like `getGraph`.
 */
export async function getGraphFocus(
  { focus, hops, limit }: { focus: string; hops?: 1 | 2 | 3; limit?: number },
  { signal }: CallOptions = {},
): Promise<FocusGraph> {
  const graph = await send(TIMEOUT_MS.read, signal, (signal) =>
    client.GET("/api/web/graph", { params: { query: { focus, hops, limit } }, signal }),
  );
  return graph as FocusGraph;
}
