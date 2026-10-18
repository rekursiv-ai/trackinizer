import type { Box } from "./markGeometry";

/** A visual on the stage with its body: the area below its toolbar, where boxes pinned to it stand. */
export type Tile = { readonly id: string; readonly rect: Box };

/**
 * The visuals on `stage` in paint order, the topmost last: the docked ones in page
 * order, then the floating ones by z-index. A visual whose body has no size on
 * screen (folded to its bar, hidden) is left out.
 */
export function tilesInPaintOrder(stage: HTMLElement): Tile[] {
  const found = [...stage.querySelectorAll<HTMLElement>("[data-visual-instance]")].flatMap((tile, order) => {
    const body = [...tile.children].find((child) => child.classList.contains("visual-tile-body"));
    if (!body) return [];
    const { left, top, width, height } = body.getBoundingClientRect();
    if (width <= 0 || height <= 0) return [];
    const floating = tile.classList.contains("visual-tile-floating");
    const lift = floating ? Number(getComputedStyle(tile).zIndex) || 0 : 0;
    return [{ id: tile.dataset.visualInstance!, rect: { left, top, width, height }, floating, lift, order }];
  });
  found.sort((a, b) => Number(a.floating) - Number(b.floating) || a.lift - b.lift || a.order - b.order);
  return found.map(({ id, rect }) => ({ id, rect }));
}
