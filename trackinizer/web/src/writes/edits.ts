import { ApiError } from "../api/client";
import { type Change, type DetailRow, getDetail, type Peer } from "../api/detail";
import type { EdgeAnnotation, EdgeRef, EdgeWrite, SetEdgeAnnotationBody } from "../api/edges";
import type { FieldRoute } from "../api/fields";
import {
  type ClearFieldPath,
  type FieldWrite,
  getInquiry,
  type PatchFieldBody,
  type PatchFieldPath,
  type SetFieldBody,
  type SetFieldPath,
} from "../api/inquiries";
import { isUnset, kindFields } from "../detail/fields";
import {
  clearEdgeAnnotationRequest,
  clearFieldRequest,
  patchEdgeLabelsRequest,
  patchFieldRequest,
  setEdgeAnnotationRequest,
  setFieldRequest,
  type WriteRequest,
} from "./requests";

/** One edit the user made, as the write layer sends it. */
export type Edit<Result = unknown> = {
  /** Its one request: a retry sends the same key and body again. */
  readonly request: WriteRequest<Result>;
  /** The rows it changes. Every cached read showing one refetches once it lands. */
  readonly touches: readonly string[];
  /**
   * The rows it makes, given its result. Once it lands, they and `touches` go
   * to the live hub as the stream's ids do, so a list they newly belong in shows
   * them, the stream down or not. A method, as `undo` is.
   */
  creates?(result: Result): readonly string[];
  /** What the toast says once it lands; without it, no toast and so no Undo. */
  readonly done?: string;
  /**
   * Set on an edit that must never be sent twice (a token create: the server
   * keeps no key for it, so a second send makes a second token). After a
   * timeout, a dropped network or a 5xx it may have landed, so it offers no
   * Retry, and this says so.
   */
  readonly maybeLanded?: string;
  /** How it guards against editing stale data: a check at save, or compare-and-set. */
  readonly guard?: Guard<Result>;
  /**
   * The one write that reverses it, given its result; none when it changed nothing.
   * A method, so an `Edit<FieldWrite>` is also an `Edit<unknown>`: it is only ever
   * called with its own request's result.
   */
  undo?(result: Result): Edit<Result> | undefined;
};

/**
 * How an edit guards against stale data: `base`, the value editing began from,
 * and what the dialogs show: the field's label, the value being saved (`mine`),
 * and `show` to turn a value into text. Unset values are `undefined`. `id` and
 * `field` name what it guards, `read` reads the value stored now, and `again`
 * makes the edit that saves `mine` over it, taking it as the new `base`. Every
 * path that sends the edit honours it.
 *
 * - `check`, for fields without compare-and-set: at save, `read` the stored value
 *   again and compare it with `base`. A write landing between the check and the
 *   save still wins; closing that gap needs compare-and-set on every field on
 *   the server.
 * - `cas`, for status, owner and judgement: the server answers 409 when the
 *   stored value is no longer `base`.
 *
 * Either way a 409 opens the conflict dialog, whether the server's or the one a
 * resend's read throws on finding someone else's value (`reconciled`).
 */
export type Guard<Result> = {
  readonly type: "check" | "cas";
  readonly id: string;
  readonly field: string;
  readonly label: string;
  readonly base: unknown;
  readonly mine: unknown;
  readonly show: (value: unknown) => string;
  readonly read: () => Promise<unknown>;
  readonly again: (base: unknown) => Edit<Result>;
};

/** One field's new value, as an editor hands it to the write layer. */
export type FieldChange = {
  readonly id: string;
  /** The field's name, `priority`, and its routes from `editableFields`. */
  readonly field: string;
  readonly route: FieldRoute;
  readonly label: string;
  /** The value editing began from, as the detail reads it: `undefined` when unset. */
  readonly from: unknown;
  /** The value to save. An unset value (`null`, `""`, `[]`) clears the field. */
  readonly to: unknown;
  readonly reason?: string;
  /** A value as text, for the toast and the dialogs; `showValue` by default. */
  readonly show?: (value: unknown) => string;
};

/**
 * The fields the server guards with compare-and-set. Their writes always send
 * `mode: "cas"` with the value the user saw, and a 409 opens the conflict dialog.
 * The schema offers `mode` on every field, so the list cannot come from it.
 */
export const CAS_FIELDS: ReadonlySet<string> = new Set(["status", "owner", "judgement"]);

/**
 * The edit that sets one field: a `PUT`, or a `DELETE` when the new value is
 * unset. Status, owner and judgement go by compare-and-set; every other field
 * checks at save that its stored value is still the one editing began from. Its
 * undo is the reverse edit, guarded the same way. A list is never set or
 * cleared whole: `listEdit` changes it one element at a time.
 *
 * A resend after a lost answer reads the field first (`reconciled`). A
 * compare-and-set field would not need it: the server replays the key of a
 * write that changed something and answers 409 to any other stale resend. It
 * reads anyway, the same rule as every write.
 */
export function fieldEdit(change: FieldChange): Edit<FieldWrite> {
  const { id, field, route, label, from, to } = change;
  if (route.value.type === "array") throw new Error(`${field} is a list: change it one element at a time with listEdit.`);
  const show = change.show ?? showValue;
  const reason = change.reason ? { reason: change.reason } : {};
  const cas = CAS_FIELDS.has(field);
  if (isUnset(to) && !cas && !route.delete) throw new Error(`${field} cannot be cleared.`);
  // The editor chose `to` from the field's value type in the field-type map, so it
  // is this route's value type; the server checks it again (422).
  const request =
    isUnset(to) && !cas
      ? clearFieldRequest(route.path as ClearFieldPath, id, reason)
      : setFieldRequest(route.path, id, {
          value: isUnset(to) ? null : to,
          ...(cas && { mode: "cas", expected: isUnset(from) ? null : from }),
          ...reason,
        } as SetFieldBody<SetFieldPath>);
  const guard: Guard<FieldWrite> = {
    type: cas ? "cas" : "check",
    id,
    field,
    label,
    base: from,
    mine: to,
    show,
    read: () => readField(id, field),
    again: (base: unknown) => fieldEdit({ ...change, from: base }),
  };
  const landed = { id, change_id: sameValue(from, to) ? null : request.key };
  return {
    request: reconciled(request, { read: guard.read, from, to, label, show, landed }),
    touches: [id],
    done: isUnset(to) ? `Cleared ${label}` : `${label} set to ${short(show(to))}`,
    guard,
    undo: (result) =>
      result.change_id === null ? undefined : withoutUndo(fieldEdit({ id, field, route, label, from: to, to: from, show })),
  };
}

/** One element added to or removed from a list field. */
export type ListChange = {
  readonly id: string;
  readonly field: string;
  readonly route: FieldRoute;
  readonly label: string;
  readonly op: "add" | "sub";
  readonly value: unknown;
  /** The list as the editor shows it, `undefined` when unset; it tells a last element. */
  readonly from?: unknown;
  readonly reason?: string;
  readonly show?: (value: unknown) => string;
};

/**
 * The edit that adds or removes one element of a list field with `PATCH`, the
 * last one too, which leaves the field unset. The server applies each under a
 * row lock, so concurrent edits to other elements all land, and no check at
 * save is needed. Its undo is the opposite operation.
 *
 * A resend after a lost answer first counts the element's copies: as many as in
 * `from` is unsent, one more (add) or one fewer (sub) is landed. An author list
 * repeats, so whether the element is there would not tell: one copy of two that
 * someone else removed would read as unsent, and the resend would remove the
 * other. A caller without `from` edits a list whose elements do not repeat.
 *
 * An Issue's last type is the exception: the server refuses to empty
 * `issue_kind` by `PATCH` (409), so `clearLastType` clears it with `DELETE`.
 */
export function listEdit(change: ListChange): Edit<FieldWrite> {
  const { id, field, route, label, op, value } = change;
  const show = change.show ?? showValue;
  if (field === "issue_kind" && op === "sub" && sameValue(change.from, [value])) return clearLastType(change, show);
  const body = { op, value, ...(change.reason && { reason: change.reason }) };
  // The element comes from the field-type map's element type, as in `fieldEdit`.
  const request = patchFieldRequest(route.path as PatchFieldPath, id, body as PatchFieldBody<PatchFieldPath>);
  const items = Array.isArray(change.from) ? (change.from as unknown[]) : [];
  const copies = (list: unknown) => (Array.isArray(list) ? list.filter((element) => sameValue(element, value)).length : 0);
  // Removing one needs one there, whether or not `from` was given.
  const before = Math.max(copies(items), op === "sub" ? 1 : 0);
  const at = items.findIndex((element) => sameValue(element, value));
  // The list its undo edits, when this edit's was known.
  const after = change.from === undefined ? undefined : op === "add" ? [...items, value] : items.filter((_, index) => index !== at);
  const landed = { id, change_id: request.key };
  return {
    request: reconciled(request, {
      read: () => readField(id, field),
      state: copies,
      from: before,
      to: op === "add" ? before + 1 : before - 1,
      label,
      landed,
    }),
    touches: [id],
    done: op === "add" ? `Added ${short(show(value))} to ${label}` : `Removed ${short(show(value))} from ${label}`,
    undo: (result) =>
      result.change_id === null
        ? undefined
        : withoutUndo(listEdit({ ...change, op: op === "add" ? "sub" : "add", from: after, reason: undefined })),
  };
}

/**
 * The edit that removes an Issue's last type: a `DELETE` of `issue_kind`. It
 * clears the whole list, so a type someone added since `from` was read would go
 * too; like a field without compare-and-set, it checks at save that the stored
 * list is still `from`, and saving over a changed one clears it. Its undo adds
 * the type back.
 */
function clearLastType(change: ListChange, show: (value: unknown) => string): Edit<FieldWrite> {
  const { id, field, route, label, value, from } = change;
  const request = clearFieldRequest(route.path as ClearFieldPath, id, change.reason ? { reason: change.reason } : {});
  const read = () => readField(id, field);
  return {
    request: reconciled(request, { read, from, to: undefined, label, show, landed: { id, change_id: request.key } }),
    touches: [id],
    done: `Removed ${short(show(value))} from ${label}`,
    guard: {
      type: "check",
      id,
      field,
      label,
      base: from,
      mine: undefined,
      show,
      read,
      again: (base: unknown) => clearLastType({ ...change, from: base }, show),
    },
    undo: (result) =>
      result.change_id === null
        ? undefined
        : withoutUndo(listEdit({ ...change, op: "add", from: undefined, reason: undefined })),
  };
}

/** A new value for one of an edge's scalar annotations. */
export type EdgeChange = {
  readonly edge: EdgeRef;
  readonly annotation: EdgeAnnotation;
  readonly label: string;
  readonly from: unknown;
  /** Unset clears it; a cleared valence reads back as 0.5. */
  readonly to: unknown;
  readonly reason?: string;
  readonly show?: (value: unknown) => string;
};

/**
 * The edit that sets or clears one edge annotation. It touches both ends, since
 * both show the edge. Its undo is the reverse edit. Annotations have no
 * compare-and-set, and the plan's check at save covers row fields only. A
 * resend after a lost answer reads the annotation first, since the server
 * replays no edge write.
 */
export function edgeAnnotationEdit(change: EdgeChange): Edit<EdgeWrite> {
  const { edge, annotation, label, from, to } = change;
  const show = change.show ?? showValue;
  const reason = change.reason ? { reason: change.reason } : {};
  const request = isUnset(to)
    ? clearEdgeAnnotationRequest(annotation, edge, reason)
    : // The value comes from the annotation's editor, typed for this annotation.
      setEdgeAnnotationRequest(annotation, edge, { value: to, ...reason } as SetEdgeAnnotationBody<EdgeAnnotation>);
  const read = async () => (await readEdge(edge))?.[annotation];
  const landed = { change_id: sameValue(from, to) ? null : request.key, created: false };
  return {
    request: reconciled(request, { read, from, to, label, show, landed }),
    touches: [edge.from, edge.to],
    done: isUnset(to) ? `Cleared ${label}` : `${label} set to ${short(show(to))}`,
    undo: (result) =>
      result.change_id === null
        ? undefined
        : withoutUndo(edgeAnnotationEdit({ ...change, from: to, to: change.from, reason: undefined })),
  };
}

/**
 * The edit that adds or removes one of an edge's labels; its undo is the
 * opposite. A resend after a lost answer first reads whether the label is there.
 */
export function edgeLabelEdit(change: { readonly edge: EdgeRef; readonly op: "add" | "sub"; readonly value: string; readonly reason?: string }): Edit<EdgeWrite> {
  const { edge, op, value } = change;
  const request = patchEdgeLabelsRequest(edge, { op, value, ...(change.reason && { reason: change.reason }) });
  const has = async () => (await readEdge(edge))?.labels?.includes(value) ?? false;
  const landed = { change_id: request.key, created: false };
  return {
    request: reconciled(request, { read: has, from: op === "sub", to: op === "add", label: "Labels", landed }),
    touches: [edge.from, edge.to],
    done: op === "add" ? `Added label ${value}` : `Removed label ${value}`,
    undo: (result) =>
      result.change_id === null ? undefined : withoutUndo(edgeLabelEdit({ ...change, op: op === "add" ? "sub" : "add", reason: undefined })),
  };
}

/**
 * `request`, made safe to resend after an attempt got no sure answer
 * (`WriteRequest.reconcile`): `read` the stored value first, and take the
 * `state` of it the write changes (the value itself, by default). `to` means the
 * write landed, and it resolves with `landed`; `from` means it did not, and it is
 * sent again under the same key. Anything else is someone else's change: a 409
 * that names the stored value, worded as the conflict toast is. For a state that
 * is only there or not (an edge, a row), `read` says whether it is.
 *
 * A state someone else changed and changed back inside the retry window, a
 * matter of seconds, reads as unsent, and the resend applies the write again:
 * it re-adds an edge they removed, or removes one they added back. The server
 * catches this only for a field write that changed something, whose key it
 * replays.
 *
 * `landed` gives the key as the change's id: the server takes a write's key as
 * the id of the change it records. A read shows the state, not who made it,
 * though: someone else may have saved the same. So an edit found landed this
 * way offers no Undo (`foundLanded`), which could overwrite theirs.
 */
export function reconciled<Result>(
  request: WriteRequest<Result>,
  {
    read,
    state = (stored) => stored,
    from,
    to,
    label,
    show = showValue,
    landed,
  }: {
    readonly read: () => Promise<unknown>;
    readonly state?: (stored: unknown) => unknown;
    readonly from: unknown;
    readonly to: unknown;
    readonly label: string;
    readonly show?: (value: unknown) => string;
    readonly landed: Result;
  },
): WriteRequest<Result> {
  return {
    ...request,
    reconcile: async () => {
      const stored = await read();
      if (sameValue(state(stored), to)) return landed;
      if (sameValue(state(stored), from)) return null;
      throw new ApiError(409, `Not saved: someone changed ${label} to ${show(stored)}`, "conflict");
    },
  };
}

/**
 * `edge` as its child's detail has it now: the peer at its far end, with the
 * edge's annotations; `undefined` when there is no such edge.
 */
export async function readEdge(edge: EdgeRef): Promise<Peer | undefined> {
  return (await getDetail(edge.from)).edges[edge.kind]?.find((peer) => peer.id === edge.to);
}

/**
 * `field`'s value in `row`, read as the detail reads it: `undefined` when unset,
 * and the cost axes from their nesting under `marginal_cost`.
 */
export function fieldValue(row: DetailRow, field: string): unknown {
  return kindFields(row, {}).find((candidate) => candidate.name === field)?.value;
}

/**
 * The newest change to `field` among `changes` (newest first). A change names a
 * field by its flat storage name: bare (`status`), or `<kind>_<field>`
 * (`belief_judgement`).
 */
export function lastChangeOf(changes: readonly Change[], field: string): Change | undefined {
  return changes.find((change) => change.kind === field || change.kind === `${change.subject_kind.toLowerCase()}_${field}`);
}

/**
 * Whether two field values are the same: every unset field value alike, else
 * equal JSON. Inside a value, `null`, `""` and `[]` differ, as they do to the
 * server.
 */
export function sameValue(a: unknown, b: unknown): boolean {
  if (isUnset(a) || isUnset(b)) return isUnset(a) && isUnset(b);
  return sameJson(a, b);
}

/** Whether two JSON values are equal, objects in any key order. */
function sameJson(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (typeof a !== "object" || typeof b !== "object" || a === null || b === null) return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  const [left, right] = [a as { [key: string]: unknown }, b as { [key: string]: unknown }];
  const keys = Object.keys(left);
  return keys.length === Object.keys(right).length && keys.every((key) => key in right && sameJson(left[key], right[key]));
}

/** A field value as text: `Not set`, a list joined by commas, or JSON for an object. */
export function showValue(value: unknown): string {
  if (isUnset(value)) return "Not set";
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map((item) => (typeof item === "string" ? item : JSON.stringify(item))).join(", ");
  return JSON.stringify(value, null, 2);
}

/** `field` as stored now. */
async function readField(id: string, field: string): Promise<unknown> {
  return fieldValue(await getInquiry(id), field);
}

/** `edit` with no undo of its own: an undo is not undone in turn. */
function withoutUndo<Result>(edit: Edit<Result>): Edit<Result> {
  return { ...edit, undo: undefined };
}

/** Text for a toast: one line of at most 60 characters. */
export function short(text: string): string {
  const line = text.replace(/\s+/g, " ").trim();
  return line.length > 60 ? `${line.slice(0, 59)}…` : line;
}
