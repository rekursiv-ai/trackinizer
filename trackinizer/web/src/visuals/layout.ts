/** Where a visual stands: the main strip, the column left of it, the column right of it ("side"), or floating over them. */
export type Placement = "main" | "left" | "side" | "floating";
type PlacedVisual = { readonly id: string; readonly placement?: Placement };

/** The two columns beside the page, in the order they stand, and the bar's button that docks a tile in each. */
export const SIDES = [
  { placement: "left", name: "left", glyph: "◧" },
  { placement: "side", name: "right", glyph: "◨" },
] as const;

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

/**
 * Where Chat stands when it is docked: a panel beside the page, in either
 * column, or in the main strip when it was placed there. Chat floats only while
 * it stands aside for what the assistant shows, which is the tab's own state and
 * never a stored placement; a canvas made when Chat floated by default still
 * stores "floating" for it, which reads as the right-hand column.
 */
export function chatHome(placement: Placement | undefined): "main" | "left" | "side" {
  return placement === "main" || placement === "left" ? placement : "side";
}
