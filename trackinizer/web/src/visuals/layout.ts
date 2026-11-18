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
