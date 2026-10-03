import { type CallOptions, client, send, TIMEOUT_MS } from "./client";
import type { components, paths } from "./generated/schema";
import type { Keyed } from "./idempotency";
import type { MutationBody } from "./inquiries";

/**
 * One edge, named as its routes name it. Edges are stored child to parent, so
 * `from` is the child: an Issue `narrows` the Issue it is `to`.
 */
export type EdgeRef = {
  readonly from: string;
  /** The stored edge kind: `narrows`, `proves`. */
  readonly kind: string;
  readonly to: string;
};

/**
 * What an edge write returns. `change_id` is null when nothing changed;
 * `created` is true only when an add made a new edge rather than annotating one.
 */
export type EdgeWrite = { change_id: string | null; created: boolean };

/**
 * The body of an edge add: the annotations to set with it, and `reason`.
 *
 * Adding an edge that exists writes these annotations onto it, so send only the
 * ones the user set. Never `actor`: the server records the signed-in user.
 */
export type AddEdgeBody = WithoutKeys<components["schemas"]["CreateEdge"], "actor">;

/**
 * The annotations set with `PUT`. Labels are a list, so they change one element
 * at a time through `patchEdgeLabels`, never as a whole list.
 */
export type EdgeAnnotation = "note" | "valence" | "priority";

/**
 * The body a `PUT` of `annotation` takes: `value` and `reason`.
 *
 * No `mode` or `expected`: no annotation has compare-and-set, and the server
 * answers 400 to one that asks for it rather than overwrite blindly.
 */
export type SetEdgeAnnotationBody<A extends EdgeAnnotation> = WithoutKeys<
  NonNullable<paths[AnnotationPath<A>]["put"]>["requestBody"]["content"]["application/json"],
  "actor" | "mode" | "expected"
>;

/** The body of a `PATCH` to an edge's labels: add or remove one label. */
export type PatchEdgeLabelsBody = WithoutKeys<components["schemas"]["FieldOp_str_"], "actor">;

/**
 * Add the edge with `POST`; the idempotency key goes in the header.
 *
 * The first structural edge between two inquiries can also add an inferred
 * `produced_by` that removing this edge leaves behind, so an add has no undo.
 */
export async function addEdge(
  edge: EdgeRef,
  write: Keyed<AddEdgeBody>,
  { signal }: CallOptions = {},
): Promise<EdgeWrite> {
  const result = await send(TIMEOUT_MS.write, signal, (signal) =>
    client.POST("/api/edges/{from_id}/{edge_kind}/{to_id}", {
      params: { path: pathOf(edge) },
      body: write.body,
      headers: { "Idempotency-Key": write.key },
      signal,
    }),
  );
  return result as EdgeWrite;
}

/** Set one annotation with `PUT`; the idempotency key goes in the header. */
export async function setEdgeAnnotation<A extends EdgeAnnotation>(
  annotation: A,
  edge: EdgeRef,
  write: Keyed<SetEdgeAnnotationBody<A>>,
  { signal }: CallOptions = {},
): Promise<EdgeWrite> {
  // Widened to every annotation route: openapi-fetch cannot resolve the request
  // type of a generic path, and the signature has already pinned this one's body.
  const route: AnnotationPath<EdgeAnnotation> = `/api/edges/{from_id}/{edge_kind}/{to_id}/${annotation}`;
  const body: SetEdgeAnnotationBody<EdgeAnnotation> = write.body;
  const result = await send(TIMEOUT_MS.write, signal, (signal) =>
    client.PUT(route, {
      params: { path: pathOf(edge) },
      body,
      headers: { "Idempotency-Key": write.key },
      signal,
    }),
  );
  return result as EdgeWrite;
}

/**
 * Clear one annotation with `DELETE`, or every label at once.
 *
 * The route requires a JSON body: send `{}` or `{reason}`. A cleared valence
 * reads back as 0.5, the server's default. The idempotency key goes in the header.
 */
export async function clearEdgeAnnotation(
  annotation: EdgeAnnotation | "labels",
  edge: EdgeRef,
  write: Keyed<MutationBody>,
  { signal }: CallOptions = {},
): Promise<EdgeWrite> {
  const result = await send(TIMEOUT_MS.write, signal, (signal) =>
    client.DELETE(`/api/edges/{from_id}/{edge_kind}/{to_id}/${annotation}`, {
      params: { path: pathOf(edge) },
      body: write.body,
      headers: { "Idempotency-Key": write.key },
      signal,
    }),
  );
  return result as EdgeWrite;
}

/** Add or remove one label with `PATCH`; the idempotency key goes in the header. */
export async function patchEdgeLabels(
  edge: EdgeRef,
  write: Keyed<PatchEdgeLabelsBody>,
  { signal }: CallOptions = {},
): Promise<EdgeWrite> {
  const result = await send(TIMEOUT_MS.write, signal, (signal) =>
    client.PATCH("/api/edges/{from_id}/{edge_kind}/{to_id}/labels", {
      params: { path: pathOf(edge) },
      body: write.body,
      headers: { "Idempotency-Key": write.key },
      signal,
    }),
  );
  return result as EdgeWrite;
}

/**
 * Remove the edge with `DELETE`. The route requires a JSON body: send `{}` or
 * `{reason}`. The idempotency key goes in the header.
 */
export async function removeEdge(
  edge: EdgeRef,
  write: Keyed<MutationBody>,
  { signal }: CallOptions = {},
): Promise<EdgeWrite> {
  const result = await send(TIMEOUT_MS.write, signal, (signal) =>
    client.DELETE("/api/edges/{from_id}/{edge_kind}/{to_id}", {
      params: { path: pathOf(edge) },
      body: write.body,
      headers: { "Idempotency-Key": write.key },
      signal,
    }),
  );
  return result as EdgeWrite;
}

type AnnotationPath<A extends EdgeAnnotation> = `/api/edges/{from_id}/{edge_kind}/{to_id}/${A}`;

/** `Body` without `Keys`, which a caller then cannot send either; distributes over a union. */
type WithoutKeys<Body, Keys extends string> = Body extends unknown
  ? Omit<Body, Keys> & { [key in Keys]?: never }
  : never;

function pathOf({ from, kind, to }: EdgeRef) {
  // Edge kinds come from `/api/meta/edges` at boot, so they are the server's own.
  return { from_id: from, edge_kind: kind as components["schemas"]["trackinizer__types__edges__Kind"], to_id: to };
}
