import { useCallback, useLayoutEffect, useRef } from "react";

/**
 * Put a box's caret where a new text wants it, once that text is in the box.
 *
 * A caller that sets the text (a history recall, a completion's pick) cannot
 * place the caret at once: the box still shows the old text until React renders
 * the new one, and a caret placed before then lands in the old text. The
 * returned function records the box and the caret; the next layout effect, after
 * the render that puts the text in, places it and forgets it, so a later render
 * leaves the caret where the person put it. Its identity is stable.
 */
export function useCaretPlacement(): (box: HTMLTextAreaElement | null, caret: number) => void {
  const due = useRef<{ readonly box: HTMLTextAreaElement; readonly caret: number } | null>(null);
  useLayoutEffect(() => {
    if (!due.current) return;
    const { box, caret } = due.current;
    due.current = null;
    box.setSelectionRange(caret, caret);
  });
  return useCallback((box, caret) => {
    if (box) due.current = { box, caret };
  }, []);
}
