import { type KeyboardEvent, useState } from "react";
import { useCaretPlacement } from "./placeCaret";

/** Chronological messages from one input's history, independent of its delivery target. */
export type InputHistory = { readonly key: string; readonly entries: readonly string[] };

type Position = InputHistory & { readonly index: number; readonly draft: string };

/** Recall a snapshot of history at text boundaries, retaining the unfinished draft. */
export function useInputHistory(history: InputHistory | undefined, text: string, edit: (text: string) => void) {
  const [position, setPosition] = useState<Position | null>(null);
  if (position && position.key !== history?.key) setPosition(null);
  const place = useCaretPlacement();
  const reset = () => setPosition(null);
  const take = (event: KeyboardEvent<HTMLTextAreaElement>): boolean => {
    if (!history || event.nativeEvent.isComposing || event.shiftKey || event.altKey || event.ctrlKey || event.metaKey) return false;
    const step = event.key === "ArrowUp" ? -1 : event.key === "ArrowDown" ? 1 : 0;
    const box = event.currentTarget;
    if (!step || box.disabled || box.selectionStart !== box.selectionEnd) return false;
    if (box.selectionStart !== (step < 0 ? 0 : text.length)) return false;
    const held = position?.key === history.key ? position : null;
    if (!held && (step > 0 || history.entries.length === 0)) return false;
    const current = held ?? { key: history.key, entries: [...history.entries], index: history.entries.length, draft: text };
    const index = Math.max(0, Math.min(current.entries.length, current.index + step));
    const next = index === current.entries.length ? current.draft : current.entries[index]!;
    event.preventDefault();
    edit(next);
    setPosition(index === current.entries.length ? null : { ...current, index });
    place(box, step < 0 ? 0 : next.length);
    return true;
  };
  return { take, reset };
}
