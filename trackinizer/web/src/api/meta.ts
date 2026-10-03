import { type CallOptions, client, send, TIMEOUT_MS } from "./client";

/**
 * Every closed set the server validates, by field: `status`, `judgement`,
 * `issue_kind`, `publication_type`, `edge_kind`, and `inquiry_kind_all`, the
 * inquiry kinds in PascalCase (`Issue`, `CodeChange`).
 */
export type Enums = { readonly [field: string]: readonly string[] };

/** The kind that owns each kind-specific field (`priority` → `issue`), lowercase. */
export type FieldOwners = { readonly [field: string]: string };

/** Which kinds an edge kind joins, stored child to parent, and its labels. */
export type EdgeRule = {
  readonly from_kinds: string[];
  readonly to_kinds: string[];
  /** The label read from the child: `narrows`. */
  readonly forward: string;
  /** The label read from the parent: `narrowed_by`. */
  readonly inverse: string;
};

/** Every edge kind's rule, by edge kind. */
export type EdgeTopology = { readonly [edgeKind: string]: EdgeRule };

/** Fetch `GET /api/meta/enums`. */
export async function getEnums({ signal }: CallOptions = {}): Promise<Enums> {
  return send(TIMEOUT_MS.read, signal, (signal) => client.GET("/api/meta/enums", { signal }));
}

/** Fetch `GET /api/meta/fields`. */
export async function getFieldOwners({ signal }: CallOptions = {}): Promise<FieldOwners> {
  return send(TIMEOUT_MS.read, signal, (signal) => client.GET("/api/meta/fields", { signal }));
}

/**
 * Fetch `GET /api/meta/edges`.
 *
 * Written by hand from `edges_route` in `server/api/meta_routes.py`: the schema
 * types each rule as a map of strings and string lists.
 */
export async function getEdgeTopology({ signal }: CallOptions = {}): Promise<EdgeTopology> {
  const topology = await send(TIMEOUT_MS.read, signal, (signal) =>
    client.GET("/api/meta/edges", { signal }),
  );
  return topology as EdgeTopology;
}
