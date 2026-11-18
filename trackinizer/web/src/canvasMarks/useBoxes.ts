import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { clearMarks, drawMark, type Mark, type NewMark, removeMark, type WorkspaceState } from "../api/workspaces";
import { useCommands } from "../commands/registry";
import { SHEET, useHash, validHash } from "../router/trail";
import { acceptWorkspace } from "../stations/cache";

/** The red boxes of the open page and what the canvas bar and the layer do with them. */
export type Boxes = {
  /** The `#/...` page the boxes are on, or null under a sheet (Settings, Admin), where none show and none are drawn. */
  readonly page: string | null;
  /** The boxes drawn on this page (those with no page show on every page). */
  readonly shown: readonly Mark[];
  readonly drawing: boolean;
  readonly toggle: () => void;
  readonly draw: (box: NewMark) => void;
  readonly remove: (id: string) => void;
  /** Clear the boxes of this page, the ones the user sees. */
  readonly clearAll: () => void;
  readonly error: string | null;
};

/**
 * Box-drawing state for `workspace`'s open page. Escape leaves drawing mode, as a
 * command registered while drawing so that it is the latest binding of the key and
 * the page's own Escape (back, for a record) does not also run. A write
 * that the server refuses sets `error`; the answers go to the query cache as any
 * canvas write's do.
 */
export function useBoxes(workspace: WorkspaceState | undefined): Boxes {
  const queryClient = useQueryClient();
  const hash = useHash();
  const page = validHash(hash) && !SHEET.test(hash) ? hash : null;
  const [wanted, setWanted] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const drawing = wanted && page !== null;
  useCommands(drawing ? [{ id: "marks.pen.down", title: "Put the pen down", keys: ["Escape"], run: () => setWanted(false) }] : []);

  const id = workspace?.id;
  const write = (call: (id: string) => Promise<WorkspaceState>, failure: string) => {
    if (!id) return;
    setError(null);
    void call(id).then((state) => acceptWorkspace(queryClient, state)).catch(() => setError(failure));
  };
  const shown = page === null ? [] : (workspace?.marks ?? []).filter((box) => box.page == null || box.page === page);
  return {
    page,
    shown,
    drawing,
    toggle: () => setWanted(!drawing),
    draw: (box) => write((to) => drawMark(to, box), "Could not save that box."),
    remove: (boxId) => write((to) => removeMark(to, boxId), "Could not remove that box."),
    clearAll: () => { if (page) write((to) => clearMarks(to, page), "Could not clear the boxes."); },
    error,
  };
}
