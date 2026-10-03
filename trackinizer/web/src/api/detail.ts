import { type CallOptions, client, send, TIMEOUT_MS } from "./client";
import { inquiryKinds } from "./inquiries";

/**
 * One inquiry as `/api/web/get` gives it: the `GET /api/inquiries/{id}` object
 * less its relation keys (`_row_to_dict` in `server/web.py`).
 *
 * Written by hand, since the schema types it as free JSON. Every field of the
 * kind is present, `null` when unset; the UI still reads every field through one
 * unset test that also takes absent, `""` and `[]`. The two cost axes arrive
 * nested, as `marginal_cost.agent_usd` and `.resource_usd`.
 */
export type DetailRow = {
  readonly id: string;
  /** PascalCase: `Issue`. */
  readonly kind: string;
  readonly seq: number;
  readonly title: string;
  readonly status: string;
  /** ISO 8601 with an offset, as are the other times. */
  readonly created: string;
  readonly modified: string;
  readonly [field: string]: unknown;
};

/**
 * The inquiry at the far end of one edge, with that edge's annotations.
 *
 * `priority`, `note`, `valence` and `labels` belong to the edge and are absent
 * when unset. `priority` is the edge's own (on `narrows` and `requires`), never
 * the neighbour's: the server sends no neighbour priority.
 */
export type Peer = {
  readonly id: string;
  readonly kind: string;
  readonly seq: number;
  readonly title: string;
  readonly status: string;
  /** A Belief's judgement; absent on other kinds. */
  readonly judgement?: string;
  readonly priority?: number;
  readonly note?: string;
  /** In `[-1, 1]` on `proves` and `favors`; below 0 argues against. */
  readonly valence?: number;
  readonly labels?: readonly string[];
};

/** Neighbours by edge kind (`narrows`), in one direction. */
export type PeersByEdge = { readonly [edgeKind: string]: readonly Peer[] };

/**
 * One side of a change: the columns it touched, by flat storage name
 * (`status`, `issue_priority`), plus `peer_*` and `edge_*` on edge events.
 */
export type Snapshot = { readonly [column: string]: unknown };

/** One `change_log` row about the inquiry. */
export type Change = {
  readonly id: string;
  readonly created: string;
  readonly actor: string;
  /** The field's flat storage name for an edit, or an event: `created`, `edge_added`. */
  readonly kind: string;
  readonly subject_kind: string;
  readonly caused_by: string | null;
  readonly reason: string;
  readonly old: Snapshot;
  readonly new: Snapshot;
};

/**
 * Everything the detail view shows of one inquiry.
 *
 * Edges are stored child to parent: `edges` are those this inquiry is the child
 * of (it `narrows` them), `backlinks` those it is the parent of.
 */
export type Detail = {
  readonly self: DetailRow;
  readonly edges: PeersByEdge;
  readonly backlinks: PeersByEdge;
  /** The last 50, newest first. */
  readonly changes: readonly Change[];
};

/** Fetch `GET /api/web/get/{id}`: the row, its edges both ways, and its last 50 changes. */
export async function getDetail(id: string, { signal }: CallOptions = {}): Promise<Detail> {
  const detail = await send(TIMEOUT_MS.read, signal, (signal) =>
    client.GET("/api/web/get/{target_id}", { params: { path: { target_id: id } }, signal }),
  );
  return detail as Detail;
}

/**
 * The id of `kind`'s inquiry number `seq`, from `GET /api/inquiries/{kind}/{seq}`.
 *
 * `/api/web/get` takes only an id, so a `Kind#seq` link resolves through this
 * first. A missing row is an `ApiError` with status 404; a kind this build does
 * not know throws before asking (`inquiryKinds`).
 */
export async function findRef(kind: string, seq: number, { signal }: CallOptions = {}): Promise<string> {
  const [one] = inquiryKinds([kind]);
  const row = await send(TIMEOUT_MS.read, signal, (signal) =>
    client.GET("/api/inquiries/{kind}/{seq}", { params: { path: { kind: one!, seq } }, signal }),
  );
  // The route answers with the whole row (`get_inquiry`), typed as free JSON.
  return (row as { id: string }).id;
}

/**
 * The evidence confidence the server derives for a Belief or an Experiment.
 *
 * It folds the currently-true `proves` edges; `favors` never count. It is not
 * the author's stored `confidence`. Any other kind is an `ApiError` with status 404.
 */
export async function getEvidenceConfidence(id: string, { signal }: CallOptions = {}): Promise<number> {
  const body = await send(TIMEOUT_MS.read, signal, (signal) =>
    client.GET("/api/inquiries/{target_id}/confidence", {
      params: { path: { target_id: id } },
      signal,
    }),
  );
  return body.confidence;
}
