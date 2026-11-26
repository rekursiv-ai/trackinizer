import { type DrawNode, isPlaced } from "./model";

/**
 * Find the topmost visible placed node whose disc contains `(gx, gy)` in
 * graph space. Iterates in reverse draw order so the last-drawn node wins.
 */
export function hitTestNode(
  nodes: readonly DrawNode[],
  gx: number,
  gy: number,
  scale: number,
): DrawNode | null {
  // Small minimum screen-pixel hit target (6 px) so tiny zoomed-out nodes stay clickable.
  const minR = 6 / Math.max(scale, 0.05);
  for (let i = nodes.length - 1; i >= 0; i--) {
    const node = nodes[i];
    if (!node || node.hidden || !isPlaced(node)) continue;
    const r = Math.max(node.look.radius, minR);
    const dx = gx - node.x;
    const dy = gy - node.y;
    if (dx * dx + dy * dy <= r * r) return node;
  }
  return null;
}
