import { ApiError } from "../api/client";
import type { DetailRow } from "../api/detail";
import { editableFields } from "../api/fields";
import { type FieldWrite, getInquiry, type InquiryRow } from "../api/inquiries";
import { isUnset } from "../detail/fields";
import { type Edit, fieldEdit, listEdit, reconciled } from "../writes/edits";
import { purgeRequest } from "../writes/requests";

/**
 * One change applied to every selected row: its `title`, for the toast and the
 * report, and the `edit` it makes to one row. Each row's edit is its own request
 * with its own key, sent even to a row the list shows with the value already:
 * the list may be stale, and the server says when nothing changed.
 */
export type BulkChange = {
  readonly title: string;
  readonly edit: (row: InquiryRow) => Edit<FieldWrite>;
};

/**
 * Set `field` to `to` on every row; an unset `to` clears it. Each row's edit is
 * guarded against its value as the list shows it, as the detail's editors
 * guard theirs: status and owner by compare-and-set, priority by a check.
 */
export function setEach(
  field: "status" | "owner" | "priority",
  to: unknown,
  { label, show, reason }: { label: string; show: (value: unknown) => string; reason?: string },
): BulkChange {
  return {
    title: isUnset(to) ? `Cleared ${label.toLowerCase()}` : `${label} set to ${show(to)}`,
    edit: (row) => {
      const from = isUnset(row[field]) ? undefined : row[field];
      const route = editableFields(row.kind)[field];
      if (!route) throw new Error(`${row.kind} has no ${field}.`);
      return fieldEdit({ id: row.id, field, route, label, from, to, reason, show });
    },
  };
}

/**
 * Add `label` to every row; when every row already has it, remove it from each
 * instead, as the mock's label menu does. One element per `PATCH`, the last one
 * too, so labels others add meanwhile stay.
 */
export function toggleLabel(label: string, rows: readonly InquiryRow[]): BulkChange {
  const remove = rows.every((row) => (row.labels ?? []).includes(label));
  return {
    title: remove ? `Removed label ${label}` : `Added label ${label}`,
    edit: (row) =>
      listEdit({ id: row.id, field: "labels", route: editableFields(row.kind).labels!, label: "Labels", op: remove ? "sub" : "add", value: label }),
  };
}

/**
 * Clear `row`'s owner by compare-and-set, before a purge: the server refuses to
 * purge an owned row (409). No toast and no undo, since the purge follows.
 */
export function clearOwnerEdit(row: DetailRow, reason: string): Edit<FieldWrite> {
  const route = editableFields(row.kind).owner!;
  const edit = fieldEdit({ id: row.id, field: "owner", route, label: "Owner", from: row.owner, to: null, reason });
  return { ...edit, done: undefined, undo: undefined };
}

/**
 * Purge `row` and its edges, with `reason`; there is no undo. A resend after a
 * lost answer first reads the row: gone (404) means the purge landed, since the
 * server answers a second purge 404 too.
 */
export function purgeEdit(row: Pick<DetailRow, "id" | "kind" | "seq">, reason: string): Edit<FieldWrite> {
  const request = purgeRequest(row.id, { reason });
  const present = async () => {
    try {
      await getInquiry(row.id);
      return true;
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return false;
      throw error;
    }
  };
  const landed = { id: row.id, change_id: request.key };
  return {
    request: reconciled(request, { read: present, from: true, to: false, label: "the row", landed }),
    touches: [row.id],
    done: `Purged ${row.kind}#${row.seq}`,
  };
}
