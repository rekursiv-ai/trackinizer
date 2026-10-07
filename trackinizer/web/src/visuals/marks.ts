import { useLayoutEffect, useRef } from "react";
import type { WorkspaceState } from "../api/workspaces";
import { type MarkKind, markAfterPaint } from "../debug/timings";

type Instance = WorkspaceState["visuals"][number];

/**
 * Mark (`trackinizer.timings()`) the revision that last changed this visual: once
 * `ready`, and after each change to what it shows, in the first frame painted
 * after the commit. `kind` is `paint` for the pane's content, `data` for the
 * data it shows. A change is to the visual's type, version, placement, record or
 * parameters; moving a floating pane is none.
 */
export function useVisualMark(
  instance: Instance | undefined,
  workspace: WorkspaceState | null,
  kind: MarkKind,
  ready = true,
): void {
  const signature = instance
    ? JSON.stringify([instance.type, instance.version, instance.placement, instance.record_id, instance.params])
    : "";
  // The revision in force when the signature changed, not a later unrelated one.
  const born = useRef({ signature, revision: workspace?.revision ?? null });
  if (born.current.signature !== signature) born.current = { signature, revision: workspace?.revision ?? null };
  useLayoutEffect(() => {
    if (instance && ready && born.current.revision !== null) markAfterPaint(born.current.revision, instance.type, kind);
    // `instance` is read only for its type, which the signature holds.
  }, [signature, ready, kind]);
}
