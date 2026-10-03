import type { EdgeRef, EdgeWrite } from "../api/edges";
import type { BatchItem } from "../api/inquiries";
import type { CreatableKind } from "../create/draft";
import { kindLook } from "../ui/kinds";
import { type Edit, readEdge, reconciled } from "../writes/edits";
import { addEdgeRequest, batchRequest, removeEdgeRequest } from "../writes/requests";
import { SUPERSEDES } from "./topology";

/**
 * The edit that adds `edge`, with no annotations: each is set afterwards as its
 * own write. Adding an edge that already exists is then a no-op the server
 * answers as success.
 *
 * No undo: the first structural edge between two inquiries can also infer a
 * `produced_by` that removing the edge leaves behind, so the inverse is not one
 * write. The picker toasts once for all it added, so the edit has no `done`.
 *
 * A resend after a lost answer first reads whether the edge is there: the
 * server replays no edge write. One someone removed inside the retry window
 * reads as never added, and the resend adds it again.
 */
export function addRelationEdit(edge: EdgeRef): Edit<EdgeWrite> {
  const request = addEdgeRequest(edge);
  const landed = { change_id: request.key, created: true };
  return {
    request: reconciled(request, { read: () => exists(edge), from: false, to: true, label: "the relation", landed }),
    touches: [edge.from, edge.to],
  };
}

/**
 * The edit that removes `edge`, with the reason given, if any. It has a confirm
 * step instead of an undo.
 *
 * A resend after a lost answer first reads whether the edge is gone. One
 * someone added back inside the retry window reads as never removed, and the
 * resend removes it again.
 */
export function removeRelationEdit(edge: EdgeRef, { done, reason }: { done: string; reason: string }): Edit<EdgeWrite> {
  const request = removeEdgeRequest(edge, reason ? { reason } : {});
  const landed = { change_id: request.key, created: false };
  return {
    request: reconciled(request, { read: () => exists(edge), from: true, to: false, label: "the relation", landed }),
    touches: [edge.from, edge.to],
    done,
  };
}

/**
 * The edit that creates a new inquiry of `old`'s kind superseding `old`, in one
 * `POST /api/inquiries/batch`: the row and its `supersedes` edge land together
 * or not at all. `old` keeps its status; the edge alone marks it superseded.
 * Its kind is one a person creates (`creatableKinds`), as any create's is.
 */
export function supersedeWithNewEdit(
  old: { readonly id: string; readonly kind: CreatableKind; readonly seq: number },
  { title, description }: { title: string; description: string },
): Edit<{ ids: string[] }> {
  const item: BatchItem = { kind: old.kind, title, ...(description && { description }) };
  return {
    request: batchRequest([item], [{ edge_kind: SUPERSEDES, from_index: 0, to_id: old.id }]),
    touches: [old.id],
    creates: ({ ids }) => ids,
    done: `Created a new ${kindLook(old.kind).one} that supersedes ${old.kind}#${old.seq}`,
  };
}

/** Whether `edge` exists now. */
async function exists(edge: EdgeRef): Promise<boolean> {
  return (await readEdge(edge)) !== undefined;
}
