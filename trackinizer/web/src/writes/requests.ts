import { ApiError, failureFields } from "../api/client";
import {
  type AddEdgeBody,
  addEdge,
  clearEdgeAnnotation,
  type EdgeAnnotation,
  type EdgeRef,
  type PatchEdgeLabelsBody,
  patchEdgeLabels,
  removeEdge,
  type SetEdgeAnnotationBody,
  setEdgeAnnotation,
} from "../api/edges";
import { type Keyed, keyed } from "../api/idempotency";
import {
  type BatchEdge,
  type BatchItem,
  type ClearFieldPath,
  type CreateBody,
  clearField,
  createBatch,
  createInquiry,
  type InquiryKind,
  keyedBatch,
  type MutationBody,
  type PatchFieldBody,
  type PatchFieldPath,
  patchField,
  purgeInquiry,
  type SetFieldBody,
  type SetFieldPath,
  setField,
} from "../api/inquiries";
import { log } from "../debug/log";

/**
 * A write route, for the log. Users, tokens and the allowlist are all `account`:
 * they write no change row, so the server keeps no key for them.
 */
export type WriteRoute =
  | "setField"
  | "clearField"
  | "patchField"
  | "create"
  | "batch"
  | "addEdge"
  | "annotateEdge"
  | "removeEdge"
  | "purge"
  | "account";

/**
 * One write request, bound to one fresh idempotency key and one frozen body.
 * Every `send` sends exactly that pair, so a second `send` is a retry of the same
 * request. A new edit, even of the same field, makes a new request.
 */
export type WriteRequest<Result> = {
  readonly route: WriteRoute;
  /**
   * The idempotency key every send carries, for the log: a batch's first item's.
   * None on an account write, for which the server keeps no key.
   */
  readonly key?: string;
  readonly send: () => Promise<Result>;
  /**
   * Read the stored state, after an attempt got no sure answer, to tell whether
   * the write landed: resolve with its result if the state is already the one it
   * makes, or null if the state is still the one it started from, so it is sent
   * again. Anything else is someone else's change: throw a 409 `ApiError` naming
   * it, which each control shows as it shows a compare-and-set conflict.
   *
   * This, not the key, is what keeps a resend from undoing another user's
   * change: the server replays a keyed retry only of a field write that changed
   * something, and of a create (`docs/design_idempotency.md`, "Known gaps").
   * Reconciling against current state instead of a stored answer
   * is the alternative to keys in the AWS Builders' Library, "Making retries
   * safe with idempotent APIs"; Google AIP-155 lets a retry return current state.
   */
  readonly reconcile?: () => Promise<Result | null>;
};

/** Set a field with `PUT`. */
export function setFieldRequest<P extends SetFieldPath>(path: P, id: string, body: SetFieldBody<P>) {
  return bind("setField", body, (write: Keyed<SetFieldBody<P>>) => setField(path, id, write));
}

/** Clear a field with `DELETE`, never a `PUT` of `""`. */
export function clearFieldRequest(path: ClearFieldPath, id: string, body: MutationBody = {}) {
  return bind("clearField", body, (write) => clearField(path, id, write));
}

/** Add or remove one element of a list field with `PATCH`. */
export function patchFieldRequest<P extends PatchFieldPath>(path: P, id: string, body: PatchFieldBody<P>) {
  return bind("patchField", body, (write: Keyed<PatchFieldBody<P>>) => patchField(path, id, write));
}

/** Create one row of `kind` (`Issue`), with its relations in the body. */
export function createRequest<Kind extends InquiryKind>(kind: Kind, body: CreateBody<Kind>) {
  return bind("create", body, (write: Keyed<CreateBody<Kind>>) => createInquiry(kind, write));
}

/**
 * Create rows and link them, all or nothing; each item gets its own key.
 *
 * Its edges carry none: a resend after a lost answer replays the items, and adds
 * again any of the edges someone removed meanwhile.
 */
export function batchRequest(items: readonly BatchItem[], edges: readonly BatchEdge[] = []): WriteRequest<{ ids: string[] }> {
  const batch = keyedBatch(items, edges);
  return { route: "batch", key: batch.items[0]?.key, send: () => createBatch(batch) };
}

/** Add an edge, with the annotations the user set. */
export function addEdgeRequest(edge: EdgeRef, body: AddEdgeBody = {}) {
  return bind("addEdge", body, (write) => addEdge(edge, write));
}

/** Set one of an edge's scalar annotations with `PUT`. */
export function setEdgeAnnotationRequest<A extends EdgeAnnotation>(
  annotation: A,
  edge: EdgeRef,
  body: SetEdgeAnnotationBody<A>,
) {
  return bind("annotateEdge", body, (write: Keyed<SetEdgeAnnotationBody<A>>) => setEdgeAnnotation(annotation, edge, write));
}

/** Clear one of an edge's annotations, or all its labels, with `DELETE`. */
export function clearEdgeAnnotationRequest(
  annotation: EdgeAnnotation | "labels",
  edge: EdgeRef,
  body: MutationBody = {},
) {
  return bind("annotateEdge", body, (write) => clearEdgeAnnotation(annotation, edge, write));
}

/** Add or remove one of an edge's labels with `PATCH`. */
export function patchEdgeLabelsRequest(edge: EdgeRef, body: PatchEdgeLabelsBody) {
  return bind("annotateEdge", body, (write) => patchEdgeLabels(edge, write));
}

/** Remove an edge. */
export function removeEdgeRequest(edge: EdgeRef, body: MutationBody = {}) {
  return bind("removeEdge", body, (write) => removeEdge(edge, write));
}

/** Purge a row and its edges. */
export function purgeRequest(id: string, body: MutationBody = {}) {
  return bind("purge", body, (write) => purgeInquiry(id, write));
}

/**
 * Send `request`, and try again after a timeout, a dropped network or a 5xx, up
 * to three times, when a resend is safe (`resendable`). Any other failure, or the
 * last one, is thrown as is.
 *
 * Whenever an attempt of this request got no sure answer, automatic or before a
 * Retry, it first reads whether the write landed (`reconcile`): landed resolves
 * with its result, unsent sends it again under the same key, and someone else's
 * change is thrown as a 409. A change landing between that read and the resend
 * is overwritten, as it would be by a first attempt.
 *
 * Logs the send (a resend after an attempt that got no sure answer), a read
 * that found it landed, each automatic retry and a final failure, by route and
 * key.
 */
export async function sendWithRetries<Result>(request: WriteRequest<Result>): Promise<Result> {
  const { route, key } = request;
  log("info", unanswered.has(request) ? "write.resend" : "write.send", { route, key });
  for (let failures = 0; ; failures++) {
    try {
      const landed = unanswered.has(request) ? await request.reconcile?.() : null;
      if (landed) {
        log("info", "write.landed", { route, key });
        if (typeof landed === "object") readBack.add(landed);
        return landed;
      }
      return await request.send();
    } catch (error) {
      if (isTransient(error)) unanswered.add(request);
      const delayMs = RETRY_DELAYS_MS[failures];
      if (delayMs === undefined || !resendable(request) || !isTransient(error)) {
        log("warn", "write.failed", { route, key, ...(error instanceof ApiError ? failureFields(error) : { error: String(error) }) });
        throw error;
      }
      log("warn", "write.retry", { route, key, attempt: failures + 2, wait_ms: delayMs, status: error.status, request_id: error.sent?.id });
      await new Promise((resolve) => setTimeout(resolve, delayMs));
    }
  }
}

/** Whether no answer came (a timeout or a dropped network) or the server failed (5xx). */
export function isTransient(error: unknown): error is ApiError {
  return error instanceof ApiError && (error.status === 0 || error.status >= 500);
}

/**
 * The waits before each automatic retry. A deploy restarts the server and runs
 * migrations at startup, so the last wait gives a restart time to finish.
 */
const RETRY_DELAYS_MS = [1000, 3000, 9000];

/**
 * Whether sending `request` again after an attempt got no sure answer is safe,
 * the one rule for every write: a read first tells whether it landed
 * (`reconcile`), or the server replays its key, returning the first answer and
 * creating nothing again. The server does that for a create, a batch's items
 * too. An account write has neither, so it is never resent on its own: a resend
 * could make a second token, or answer 404 to a revoke that landed.
 *
 * The trax CLI's own retries resend without such a read.
 */
function resendable(request: WriteRequest<unknown>): boolean {
  return request.reconcile !== undefined || request.route === "create" || request.route === "batch";
}

/** Requests whose last attempt got no sure answer, so the next reads before it sends. */
const unanswered = new WeakSet<WriteRequest<unknown>>();

/**
 * Whether `result` is one a read found landed (`reconcile`), not one the server
 * answered. The read shows the state, not who made it, so it offers no Undo.
 */
export function foundLanded(result: unknown): boolean {
  return typeof result === "object" && result !== null && readBack.has(result);
}

/** Results a read found landed. */
const readBack = new WeakSet<object>();

/** A request that sends `body`, frozen under one fresh key, through `call`. */
function bind<Body, Result>(
  route: WriteRoute,
  body: Body,
  call: (write: Keyed<Body>) => Promise<Result>,
): WriteRequest<Result> & { readonly key: string } {
  const write = keyed(body);
  return { route, key: write.key, send: () => call(write) };
}
