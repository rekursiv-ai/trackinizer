/** Where a visual stands: the main strip, the column left of it, the column right of it ("side"), or floating over them. */
export type Placement = "main" | "left" | "side" | "floating";
type PlacedVisual = { readonly id: string; readonly placement?: Placement };

/** The two columns beside the page, in the order they stand, and the bar's button that docks a tile in each. */
export const SIDES = [
  { placement: "left", name: "left", glyph: "◧" },
  { placement: "side", name: "right", glyph: "◨" },
] as const;

/** A placed visual that may show a record. */
type RecordVisual = PlacedVisual & { readonly type: string; readonly record_id?: string | null };

/** The most record panes the canvas arranges itself (`MAX_RECORD_PANES` in `server/visuals/workspaces.py`). */
const RECORD_PANES = 3;

/**
 * Record panes as the canvas draws them while they are all it docks (the page
 * closed): one fills it, two stand side by side, and three put the focused one
 * big on the left and the other two stacked on the right, in the order they
 * stand. Any other canvas, and every floating window, is drawn as placed.
 */
export function arrangeRecordPanes<T extends RecordVisual>(visuals: readonly T[], focusedId: string | null): T[] {
  if (!onlyRecordPanes(visuals)) return [...visuals];
  const docked = visuals.filter((visual) => (visual.placement ?? "main") !== "floating");
  const big = docked.length === RECORD_PANES ? docked.find((visual) => visual.id === focusedId) ?? docked[0]! : null;
  return visuals.map((visual) => ((visual.placement ?? "main") === "floating"
    ? visual
    : { ...visual, placement: big && visual !== big ? "side" : "main" } as T));
}

/** Whether the canvas docks only record panes, one to three: the canvas `arrangeRecordPanes` lays out. */
export function onlyRecordPanes(visuals: readonly RecordVisual[]): boolean {
  const docked = visuals.filter((visual) => (visual.placement ?? "main") !== "floating");
  return docked.length > 0 && docked.length <= RECORD_PANES && docked.every((visual) => visual.record_id);
}

/** Keep primary panes first and the focused floating pane on top. */
export function orderVisuals<T extends PlacedVisual>(visuals: readonly T[], focusedId: string | null): T[] {
  const order = { main: 0, left: 1, side: 2, floating: 3 };
  return [...visuals].sort((left, right) => {
    const byPlacement = order[left.placement ?? "main"] - order[right.placement ?? "main"];
    if (byPlacement !== 0) return byPlacement;
    if (left.placement === "floating") {
      if (left.id === focusedId) return 1;
      if (right.id === focusedId) return -1;
    }
    return 0;
  });
}
