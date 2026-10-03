import { type CallOptions, client, send, TIMEOUT_MS } from "./client";
import type { DetailRow } from "./detail";
import type { components, paths } from "./generated/schema";
import { type Keyed, keyed } from "./idempotency";

/**
 * One row of `GET /api/inquiries`: the fields lists read.
 *
 * Written by hand from `types/inquiries.py`, since the schema types rows as free
 * JSON. Unset fields are `null`; kind-specific fields are absent on other kinds.
 * A row asked for without `fields` carries more (descriptions, every edge list)
 * than lists read; asked for with `INQUIRY_ROW_FIELDS`, it carries these alone.
 */
export type InquiryRow = {
  readonly id: string;
  /** PascalCase: `Issue`. */
  readonly kind: string;
  readonly seq: number;
  readonly title: string;
  readonly status: string;
  readonly owner: string | null;
  readonly labels: readonly string[] | null;
  readonly marginal_cost: { readonly agent_usd: number; readonly resource_usd: number };
  /** ISO 8601 with an offset, as are the other times. */
  readonly created: string;
  readonly modified: string;
  // Issue
  readonly priority?: number | null;
  readonly issue_kind?: readonly string[] | null;
  // Belief
  readonly judgement?: string | null;
  readonly confidence?: number | null;
  readonly proved_by?: readonly Citation[];
  readonly favored_by?: readonly Citation[];
  // Paper
  readonly authors?: readonly string[] | null;
  readonly venue?: string | null;
  readonly publish_date?: string | null;
  // CodeChange, WebResult, WebSearch
  readonly sha?: string | null;
  readonly url?: string | null;
  readonly provider?: string | null;
  // AgentSession
  readonly cli?: string | null;
  readonly ended?: string | null;
};

/** A citation edge on a Belief or Experiment row; `valence` below 0 argues against. */
export type Citation = { readonly id: string; readonly kind: string; readonly valence: number };

/**
 * One Issue above a row in its `narrows` ancestry, sent with `ancestors: "narrows"`.
 *
 * A row's ancestors come nearest first, each once. `child_ids` names which of the
 * row and its other ancestors narrow this one, so a row with several parents, or
 * an ancestor with several, keeps every link. The server walks at most 8 levels up
 * and sends at most 200 ancestors per response, nearest first.
 */
export type Ancestor = {
  readonly id: string;
  readonly kind: string;
  readonly seq: number;
  readonly title: string;
  readonly status: string;
  readonly child_ids: readonly string[];
};

/** A list row with the keys `Field` names, and its ancestry when the list asked for it. */
export type ListedRow<Field extends keyof InquiryRow = keyof InquiryRow> = Pick<InquiryRow, Field> & {
  readonly ancestors?: readonly Ancestor[];
};

/**
 * Every key of `InquiryRow`, once. The mapped type makes `tsc` fail when
 * `InquiryRow` gains or loses a key.
 */
const ROW_KEYS: { readonly [Key in keyof InquiryRow]-?: true } = {
  id: true,
  kind: true,
  seq: true,
  title: true,
  status: true,
  owner: true,
  labels: true,
  marginal_cost: true,
  created: true,
  modified: true,
  priority: true,
  issue_kind: true,
  judgement: true,
  confidence: true,
  proved_by: true,
  favored_by: true,
  authors: true,
  venue: true,
  publish_date: true,
  sha: true,
  url: true,
  provider: true,
  cli: true,
  ended: true,
};

/**
 * The `fields` a list asks for: every key of `InquiryRow`, so its rows carry
 * what lists read and nothing more. Fifty production rows cut to these keys
 * measured 21 KB of JSON, against 315 KB whole.
 */
export const INQUIRY_ROW_FIELDS = Object.freeze(Object.keys(ROW_KEYS)) as readonly (keyof InquiryRow)[];

/** An inquiry kind as the schema names it, PascalCase: `Issue`, `CodeChange`. */
export type InquiryKind = components["schemas"]["InquiryKind"];

/** Whether the schema this build is typed against names `kind`. */
export function isInquiryKind(kind: string): kind is InquiryKind {
  return Object.hasOwn(INQUIRY_KINDS, kind);
}

/**
 * `kinds`, each checked against the schema this build is typed against.
 *
 * The app's kinds are the server's (`/api/meta/enums`, rows' `kind`). The deploy
 * refuses a build whose schema differs from the live server's, so a kind the
 * schema does not name means this build and its server are out of step; the
 * error says so rather than letting the server answer 422.
 */
export function inquiryKinds(kinds: readonly string[]): InquiryKind[] {
  return kinds.map((kind) => {
    if (!isInquiryKind(kind)) throw new Error(`This build does not know the inquiry kind ${kind}; reload to get the server's build.`);
    return kind;
  });
}

/** One page of a list: rows of `kinds` matching every filter, newest created first. */
export type ListPage<Field extends keyof InquiryRow = keyof InquiryRow> = {
  readonly kinds: readonly InquiryKind[];
  /**
   * Each a `{field, op, value}` filter; they AND together. On Issues, `narrows`
   * filters on parents: `isnull` keeps the roots, `is <id>` one Issue's children.
   */
  readonly filters: readonly { readonly field: string; readonly op: string; readonly value: string }[];
  /** Rows per kind, as is `offset`: each kind is its own query on the server. */
  readonly limit: number;
  readonly offset: number;
  /**
   * The only keys each row carries; unset, every key. A key another kind owns
   * is absent from this kind's rows. The server reads the edges only when an
   * edge list (`proved_by`, `favored_by`) is named.
   */
  readonly fields?: readonly Field[];
  /** `narrows` adds each row's `ancestors`, whatever `fields` names. */
  readonly ancestors?: "narrows";
};

/**
 * Fetch one page of `GET /api/inquiries`.
 *
 * The server answers 400 for the whole request when a requested kind lacks a
 * filtered field; `compileQuery` never asks for one.
 */
export async function listInquiries<Field extends keyof InquiryRow = keyof InquiryRow>(
  { kinds, filters, limit, offset, fields, ancestors }: ListPage<Field>,
  { signal }: CallOptions = {},
): Promise<ListedRow<Field>[]> {
  const rows = await send(TIMEOUT_MS.read, signal, (signal) =>
    client.GET("/api/inquiries", {
      params: {
        query: {
          kind: [...kinds],
          filter: filters.map((f) => JSON.stringify(f)),
          limit,
          offset,
          ...(fields && { fields: [...fields] }),
          ...(ancestors && { ancestors }),
        },
      },
      signal,
    }),
  );
  return rows as ListedRow<Field>[];
}

/**
 * Fetch the rows of `kinds` that match every filter and whose `seq` falls in one
 * of `seqRanges`, each an inclusive `a..b`, `limit` rows per kind.
 *
 * A seq names a row only within its kind, and the server applies every range to
 * every kind, so a request for two kinds can return rows that were not asked for.
 */
export async function listInquiriesBySeq<Field extends keyof InquiryRow = keyof InquiryRow>(
  { kinds, filters, seqRanges, limit, fields, ancestors }: Omit<ListPage<Field>, "offset"> & { readonly seqRanges: readonly string[] },
  { signal }: CallOptions = {},
): Promise<ListedRow<Field>[]> {
  const rows = await send(TIMEOUT_MS.read, signal, (signal) =>
    client.GET("/api/inquiries", {
      params: {
        query: {
          kind: [...kinds],
          filter: filters.map((f) => JSON.stringify(f)),
          seq_range: [...seqRanges],
          limit,
          ...(fields && { fields: [...fields] }),
          ...(ancestors && { ancestors }),
        },
      },
      signal,
    }),
  );
  return rows as ListedRow<Field>[];
}

/** Every per-field route, `/api/<owner>/{target_id}/<field>`. */
type FieldPath = Extract<keyof paths, `/api/${string}/{target_id}/${string}`>;

/** A field route that takes a `PUT`, which sets the field. */
export type SetFieldPath = {
  [P in FieldPath]: undefined extends paths[P]["put"] ? never : P;
}[FieldPath];

/** A field route that takes a `DELETE`, which clears the field. */
export type ClearFieldPath = {
  [P in FieldPath]: undefined extends paths[P]["delete"] ? never : P;
}[FieldPath];

/**
 * The body a field's `PUT` takes: `value`, `mode: "cas"` with `expected` for
 * compare-and-set, and `reason`. Never `actor`: the server records the signed-in
 * user, and a free-text actor would override it.
 */
export type SetFieldBody<P extends SetFieldPath> = WithoutActor<
  NonNullable<paths[P]["put"]>["requestBody"]["content"]["application/json"]
>;

/**
 * The body of a `DELETE` that clears a field or purges a row: `{}` or `{reason}`.
 * Never `actor`, even in a body built apart: the server records one it is sent
 * in place of the signed-in user.
 */
export type MutationBody = WithoutActor<components["schemas"]["FieldMutation"]>;

/** What a field write returns; `change_id` is null when nothing changed. */
export type FieldWrite = { id: string; change_id: string | null };

/** The new row's id. */
export type Created = { id: string };

/** Set one field with `PUT`; the idempotency key goes in the header. */
export async function setField<P extends SetFieldPath>(
  path: P,
  targetId: string,
  write: Keyed<SetFieldBody<P>>,
  { signal }: CallOptions = {},
): Promise<FieldWrite> {
  // Widened to every field route: openapi-fetch cannot resolve the request type of
  // a generic path, and the signature has already pinned this route's body.
  const route: SetFieldPath = path;
  const body: SetFieldBody<SetFieldPath> = write.body;
  const result = await send(TIMEOUT_MS.write, signal, (signal) =>
    client.PUT(route, {
      params: { path: { target_id: targetId } },
      body,
      headers: { "Idempotency-Key": write.key },
      signal,
    }),
  );
  return result as FieldWrite;
}

/**
 * Clear one field with `DELETE`, never a `PUT` of `""`.
 *
 * The route requires a JSON body: without one it answers 422. Send `{}`, or
 * `{reason}`. The idempotency key goes in the header.
 */
export async function clearField(
  path: ClearFieldPath,
  targetId: string,
  write: Keyed<MutationBody>,
  { signal }: CallOptions = {},
): Promise<FieldWrite> {
  const result = await send(TIMEOUT_MS.write, signal, (signal) =>
    client.DELETE(path, {
      params: { path: { target_id: targetId } },
      body: write.body,
      headers: { "Idempotency-Key": write.key },
      signal,
    }),
  );
  return result as FieldWrite;
}

/**
 * The body that creates an inquiry of `Kind`: the kind's `Submit<Kind>` model,
 * less `kind`, which the route names, and the idempotency key, which is added per
 * request. Never `actor`.
 *
 * The route itself is typed as free JSON, since one route takes every kind; the
 * batch route's items name each kind's model, and those are the models the route
 * validates with (`SUBMIT_BODY` in `server/api/submit.py`). The server drops an
 * unknown key without a word, so only these types keep one out.
 */
export type CreateBody<Kind extends InquiryKind> = WithoutActor<Omit<Extract<SubmitItem, { kind: Kind }>, "kind" | "idempotency_key">>;

/**
 * Create one inquiry of `kind` with `POST /api/inquiries/<kind>`, the route taking
 * the kind in lowercase (`issue`).
 *
 * The body takes fields and relations directly, so a create with relations is one
 * atomic request. The idempotency key goes in the body as `idempotency_key`. A
 * key the body's model lacks does not compile, nor does a value of the wrong type.
 */
export async function createInquiry<Kind extends InquiryKind, Body extends CreateBody<Kind>>(
  kind: Kind,
  write: Keyed<Body & Exact<Body, CreateBody<Kind>>>,
  { signal }: CallOptions = {},
): Promise<Created> {
  const result = await send(TIMEOUT_MS.write, signal, (signal) =>
    client.POST("/api/inquiries/{kind}", {
      params: { path: { kind: kind.toLowerCase() } },
      body: { ...write.body, idempotency_key: write.key },
      signal,
    }),
  );
  return result as Created;
}

/** A field route that takes a `PATCH`: a list field, or a cost axis. */
export type PatchFieldPath = {
  [P in FieldPath]: undefined extends paths[P]["patch"] ? never : P;
}[FieldPath];

/**
 * The body a field's `PATCH` takes: `op` and one element as `value`, and `reason`.
 * On a cost axis `value` is an amount that `add` adds and `sub` subtracts.
 */
export type PatchFieldBody<P extends PatchFieldPath> = WithoutActor<
  NonNullable<paths[P]["patch"]>["requestBody"]["content"]["application/json"]
>;

/**
 * Add or remove one element of a list field with `PATCH`, never a `PUT` of the
 * whole list, so concurrent edits to other elements all land. The idempotency
 * key goes in the header.
 */
export async function patchField<P extends PatchFieldPath>(
  path: P,
  targetId: string,
  write: Keyed<PatchFieldBody<P>>,
  { signal }: CallOptions = {},
): Promise<FieldWrite> {
  // Widened to every patch route: openapi-fetch cannot resolve the request type of
  // a generic path, and the signature has already pinned this route's body.
  const route: PatchFieldPath = path;
  const body: PatchFieldBody<PatchFieldPath> = write.body;
  const result = await send(TIMEOUT_MS.write, signal, (signal) =>
    client.PATCH(route, {
      params: { path: { target_id: targetId } },
      body,
      headers: { "Idempotency-Key": write.key },
      signal,
    }),
  );
  return result as FieldWrite;
}

/** One new row in a batch, by its `kind` (`Issue`); its key is added per item. */
export type BatchItem = WithoutActor<Omit<SubmitItem, "idempotency_key">>;

/** One edge in a batch: a new row by `from_index` or `to_index`, an existing one by `from_id` or `to_id`. */
export type BatchEdge = components["schemas"]["BatchEdge"];

/**
 * A batch's body, frozen, with a fresh idempotency key per item. Make one per
 * request; to retry the batch, send the same one again.
 */
export type KeyedBatch = { readonly items: readonly Keyed<BatchItem>[]; readonly edges: readonly BatchEdge[] };

/** Pair each item with a fresh key, and freeze the whole batch. */
export function keyedBatch(items: readonly BatchItem[], edges: readonly BatchEdge[] = []): KeyedBatch {
  // The edges are frozen the way a keyed body is; the batch sends no key of its
  // own, since the server takes one per item and none for the request.
  return Object.freeze({ items: Object.freeze(items.map((item) => keyed(item))), edges: keyed(edges).body });
}

/**
 * Create rows and the edges between them, and to existing rows, in one
 * transaction with `POST /api/inquiries/batch`: all of it lands, or none.
 *
 * Each item carries its own `idempotency_key` in the body, so a retry returns the
 * ids first created instead of creating the rows again. Returns the new ids in
 * item order.
 */
export async function createBatch(batch: KeyedBatch, { signal }: CallOptions = {}): Promise<{ ids: string[] }> {
  const result = await send(TIMEOUT_MS.write, signal, (signal) =>
    client.POST("/api/inquiries/batch", {
      body: {
        items: batch.items.map(({ key, body }) => ({ ...body, idempotency_key: key })),
        edges: [...batch.edges],
      },
      signal,
    }),
  );
  return result as { ids: string[] };
}

/**
 * Purge an inquiry and its edges with `DELETE`. The route requires a JSON body:
 * send `{}` or `{reason}`. The idempotency key goes in the header.
 *
 * The server refuses to purge a row that has an owner (409); clear it first.
 */
export async function purgeInquiry(
  targetId: string,
  write: Keyed<MutationBody>,
  { signal }: CallOptions = {},
): Promise<FieldWrite> {
  const result = await send(TIMEOUT_MS.write, signal, (signal) =>
    client.DELETE("/api/inquiries/{target_id}", {
      params: { path: { target_id: targetId } },
      body: write.body,
      headers: { "Idempotency-Key": write.key },
      signal,
    }),
  );
  return result as FieldWrite;
}

/**
 * Fetch one row by id with `GET /api/inquiries/{id}`: every field of its kind,
 * `null` when unset, plus its relation keys. A purged row is a 404.
 */
export async function getInquiry(targetId: string, { signal }: CallOptions = {}): Promise<DetailRow> {
  const row = await send(TIMEOUT_MS.read, signal, (signal) =>
    client.GET("/api/inquiries/{target_id}", { params: { path: { target_id: targetId } }, signal }),
  );
  // The route answers with the whole row (`get_inquiry`), typed as free JSON.
  return row as DetailRow;
}

/** A request body without `actor`: the server records the signed-in user. */
type WithoutActor<Body> = Body extends unknown
  ? Omit<Body, "actor"> & { actor?: never }
  : never;

/** One kind's create body as the schema has it, `kind` included. */
type SubmitItem = components["schemas"]["SubmitBatch"]["items"][number];

/**
 * No key of `Body` beyond those of `Allowed`. A body built apart and passed
 * through `keyed` is not an object literal where `tsc` checks for excess keys,
 * so each extra key is made `never` instead.
 */
type Exact<Body, Allowed> = { readonly [Key in Exclude<keyof Body, keyof Allowed>]: never };

/**
 * Every kind the schema names, once. The mapped type makes `tsc` fail when the
 * schema gains or loses a kind, so this cannot drift from it; the schema, in turn,
 * is the live server's (the drift pytest and the deploy's schema check).
 */
const INQUIRY_KINDS: { readonly [Kind in InquiryKind]: true } = {
  Issue: true,
  Artifact: true,
  Experiment: true,
  Paper: true,
  Belief: true,
  CodeChange: true,
  WebResult: true,
  WebSearch: true,
  AgentSession: true,
};
