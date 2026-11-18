import { type RefObject, useEffect } from "react";

/**
 * Close a top bar menu that is `open`: on a press outside `root`, and on Esc,
 * which gives focus back to its `button`. A blur alone does not close it:
 * Safari focuses no button that is clicked, so a click inside would blur
 * toward nothing and lose itself.
 */
export function useDismiss(open: boolean, close: () => void, root: RefObject<HTMLElement | null>, button: RefObject<HTMLElement | null>): void {
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !root.current?.contains(event.target)) close();
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      event.preventDefault();
      close();
      button.current?.focus();
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [open, close, root, button]);
}
