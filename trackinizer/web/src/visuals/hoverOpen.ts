import { type PointerEvent, type RefObject, useEffect, useLayoutEffect, useRef, useState } from "react";

/** How long the pointer rests on what is folded before it opens: a pointer only crossing it opens nothing. */
export const OPEN_AFTER_MS = 120;

/** How long it stays open once neither the pointer nor the keyboard is in it. */
export const FOLD_AFTER_MS = 300;

/**
 * How long the sidebar's opened rail stays open once both have left. The opened rail
 * touches the rail, with no gap a pointer must cross, so a pointer off it has left
 * it: the wait only forgives an overshoot of the edge.
 */
export const RAIL_FOLD_AFTER_MS = 100;

/**
 * Whether something that folds on its own stands open: while the pointer is over
 * the tile (or over the strip that opens it, which takes `handlers`) or the
 * keyboard is in its contents, and until `foldAfterMs` (`FOLD_AFTER_MS` unless
 * given) after both have left.
 * What holds it open is read when the wait ends, not when it starts, so a
 * pointer that left and came back, or a focus that moved within it, folds
 * nothing. A touch has no hover: it opens and folds by `set`.
 *
 * The pointer counts once it moves on the tile, not when the tile comes to lie
 * under it: a page that loads, or a layout that shifts, under a pointer resting
 * at the screen's edge sends the tile an enter with no move, and the user, who
 * moved nothing, asked for nothing.
 *
 * The tile is watched by the page's own pointer and focus events, not React's.
 * React tells an element of these by the component tree, where what a portal
 * draws into the tile (the canvas's controls, in the sidebar) is not in it, so
 * the pointer resting on those would fold the tile. While `enabled` is false the
 * tile is not watched: the pointer on it holds nothing open.
 */
export function useHoverOpen(
  tile: RefObject<HTMLElement | null>,
  { enabled = true, foldAfterMs = FOLD_AFTER_MS }: { readonly enabled?: boolean; readonly foldAfterMs?: number } = {},
): {
  readonly open: boolean;
  /** Open or fold at once. Folding takes the keyboard out of the tile; what was typed in it stays. */
  readonly set: (open: boolean) => void;
  /** The handlers of the strip that opens the tile, which holds it open as the tile does. */
  readonly handlers: {
    readonly onPointerMove: (event: PointerEvent) => void;
    readonly onPointerLeave: (event: PointerEvent) => void;
    readonly onBlur: () => void;
  };
} {
  const [open, setOpen] = useState(false);
  const hovered = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  // A ref, as the tile's listeners keep the first render's handlers.
  const foldAfter = useRef(foldAfterMs);
  foldAfter.current = foldAfterMs;
  useEffect(() => () => clearTimeout(timer.current), []);
  // These read only refs and `setOpen`, so the tile's listeners may keep the first render's.
  const settle = (afterMs: number) => {
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setOpen(hovered.current || typing(tile.current)), afterMs);
  };
  const hover = (on: boolean, pointerType: string) => {
    if (pointerType === "touch") return;
    hovered.current = on;
    settle(on ? OPEN_AFTER_MS : foldAfter.current);
  };
  // Only the first move counts: the rest would put off the opening, or open again what `set` folded.
  const moved = (pointerType: string) => {
    if (!hovered.current) hover(true, pointerType);
  };
  // The tile's element, followed after each commit: a tile mounted again elsewhere,
  // as the sidebar's rail is when the canvas comes, is watched where it now stands.
  const [element, setElement] = useState<HTMLElement | null>(null);
  useLayoutEffect(() => {
    if (tile.current !== element) setElement(tile.current);
  });
  useEffect(() => {
    if (!enabled || !element) return;
    const move = (event: globalThis.PointerEvent) => moved(event.pointerType);
    const leave = (event: globalThis.PointerEvent) => hover(false, event.pointerType);
    const blur = () => settle(foldAfter.current);
    element.addEventListener("pointermove", move);
    element.addEventListener("pointerleave", leave);
    element.addEventListener("focusout", blur);
    return () => {
      element.removeEventListener("pointermove", move);
      element.removeEventListener("pointerleave", leave);
      element.removeEventListener("focusout", blur);
      // No leave comes once the tile is not watched, so a pointer on it now holds nothing.
      hovered.current = false;
    };
  }, [enabled, element]);
  return {
    open,
    set: (next) => {
      const active = document.activeElement;
      if (!next && typing(tile.current) && active instanceof HTMLElement) active.blur();
      // After the blur, whose own wait would open the tile again under a pointer still on it.
      clearTimeout(timer.current);
      setOpen(next);
    },
    handlers: {
      onPointerMove: (event) => moved(event.pointerType),
      onPointerLeave: (event) => hover(false, event.pointerType),
      onBlur: () => settle(foldAfter.current),
    },
  };
}

/** Whether the keyboard is in the tile's contents. */
function typing(tile: HTMLElement | null): boolean {
  const active = document.activeElement;
  return tile !== null && active !== null && tile.contains(active);
}
