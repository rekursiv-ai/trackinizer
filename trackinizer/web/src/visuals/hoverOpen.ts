import { type PointerEvent, type RefObject, useEffect, useRef, useState } from "react";

/** How long the pointer rests on the folded tile before it opens: a pointer only crossing its bar opens nothing. */
export const OPEN_AFTER_MS = 120;

/** How long the tile stays open once neither the pointer nor the keyboard is in it. */
export const FOLD_AFTER_MS = 300;

/**
 * Whether a tile that folds on its own stands open: while the pointer is over
 * it or the keyboard is in its contents, and until `FOLD_AFTER_MS` after both
 * have left. What holds it open is read when the wait ends, not when it starts,
 * so a pointer that left and came back, or a focus that moved within the tile,
 * folds nothing. A touch has no hover: it opens and folds the tile by `set`, as
 * the tile's bar and its fold button do.
 */
export function useHoverOpen(tile: RefObject<HTMLElement | null>): {
  readonly open: boolean;
  /** Open or fold at once. Folding takes the keyboard out of the tile; what was typed in it stays. */
  readonly set: (open: boolean) => void;
  /** The pointer is on the tile already, as after the drop that put it there: open until it leaves. */
  readonly enter: () => void;
  /** The tile's own handlers. */
  readonly handlers: {
    readonly onPointerEnter: (event: PointerEvent) => void;
    readonly onPointerLeave: (event: PointerEvent) => void;
    readonly onBlur: () => void;
  };
} {
  const [open, setOpen] = useState(false);
  const hovered = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  const settle = (afterMs: number) => {
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setOpen(hovered.current || typing(tile.current)), afterMs);
  };
  return {
    open,
    set: (next) => {
      const active = document.activeElement;
      if (!next && typing(tile.current) && active instanceof HTMLElement) active.blur();
      // After the blur, whose own wait would open the tile again under a pointer still on its bar.
      clearTimeout(timer.current);
      setOpen(next);
    },
    enter: () => {
      clearTimeout(timer.current);
      hovered.current = true;
      setOpen(true);
    },
    handlers: {
      onPointerEnter: (event) => {
        if (event.pointerType === "touch") return;
        hovered.current = true;
        settle(OPEN_AFTER_MS);
      },
      onPointerLeave: (event) => {
        if (event.pointerType === "touch") return;
        hovered.current = false;
        settle(FOLD_AFTER_MS);
      },
      onBlur: () => settle(FOLD_AFTER_MS),
    },
  };
}

/** Whether the keyboard is in the tile's contents. Its bar does not count: a button pressed there would hold the tile open. */
function typing(tile: HTMLElement | null): boolean {
  const active = document.activeElement;
  return tile !== null && active !== null && tile.contains(active) && active.closest(".visual-tile-toolbar") === null;
}
