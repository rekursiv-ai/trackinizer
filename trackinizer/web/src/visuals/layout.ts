type PlacedVisual = { readonly id: string; readonly placement?: "main" | "side" | "floating" };

/** Keep primary panes first and the focused floating pane on top. */
export function orderVisuals<T extends PlacedVisual>(visuals: readonly T[], focusedId: string | null): T[] {
  const order = { main: 0, side: 1, floating: 2 };
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
