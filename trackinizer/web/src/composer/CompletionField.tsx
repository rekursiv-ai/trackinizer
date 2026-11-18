import { type CSSProperties, type KeyboardEvent, type ReactNode, type RefObject, useId, useLayoutEffect, useRef, useState } from "react";
import { caretPoint, menuLeft } from "./caretPoint";
import type { FieldProps } from "./Composer";
import { useCaretPlacement } from "./placeCaret";
import "../lists/lists.css";
import "./CompletionField.css";

/** The word the caret is in that a list may complete: `start` to `end`, and the text from after its sigil to the caret. */
export type Target = { readonly start: number; readonly end: number; readonly query: string };

/** One option: `insert` replaces the target; `title` names it; `label` draws it. */
export type Completion = { readonly key: string; readonly title: string; readonly label: ReactNode; readonly insert: string };

/**
 * A Composer `field` that completes the word `find` sees at the caret, from
 * the options its caller computes for the target, as a menu over the box.
 *
 * `useCompletion` returns the open `target` (null while the caret is in none,
 * the box lacks focus or Esc or a pick closed it) and `render`, which draws the
 * box with the caller's options. While options show, the textarea is a combobox
 * naming the active one (`aria-activedescendant`); ↑ and ↓ move, wrapping;
 * Enter or Tab takes the active one, and a click any; Esc closes the list until
 * the caret is in another target or before where it closed. A key held with
 * Shift, Alt, Ctrl or Meta, and any key while an input
 * method composes, is the composer's: Alt+Enter never picks.
 *
 * A pick puts `insert` in the target's place, and a space after it at the end of
 * the text; the caret goes past that space, or past the whitespace already there.
 * The list stays closed on the picked word: `insert` as `find` reads it there.
 * `target` is the very object `find` made, so a caller's own fields on it come back.
 *
 * The list, and the empty hint, open just above the caret's line, starting at
 * the caret (`caretPoint`), and follow it as the box scrolls or resizes and the
 * window resizes; a menu that would run past the viewport's right edge moves
 * left (`menuLeft`), overhanging a narrow box rather than shrinking into it.
 */
export function useCompletion<T extends Target = Target>({
  field,
  edit,
  find,
}: {
  field: FieldProps;
  edit: (text: string) => void;
  find: (value: string, caret: number) => T | null;
}): { target: T | null; render: (options: readonly Completion[], label: string, emptyHint?: string) => ReactNode } {
  const listId = useId();
  const box = useRef<HTMLTextAreaElement>(null);
  const [caret, setCaret] = useState(0);
  const [focused, setFocused] = useState(false);
  const [closed, setClosed] = useState<{ readonly start: number; readonly query: string } | null>(null);
  const [active, setActive] = useState({ at: "", index: 0 });
  const placeCaret = useCaretPlacement();
  const holder = useRef<HTMLDivElement>(null);
  const menu = useRef<HTMLElement>(null);
  const found = find(field.value, caret);
  const shut = !found || !focused || field.disabled || (closed?.start === found.start && found.query.startsWith(closed.query));
  const target = shut ? null : found;
  const place = useMenuPlace(box, holder, menu, target === null ? null : caret);

  const track = ({ value, selectionStart }: HTMLTextAreaElement) => {
    setCaret(selectionStart);
    if (!find(value, selectionStart)) setClosed(null);
  };
  useLayoutEffect(() => {
    if (!focused) return;
    const select = () => {
      const field = box.current;
      if (field) setCaret(field.selectionStart);
    };
    document.addEventListener("selectionchange", select);
    return () => document.removeEventListener("selectionchange", select);
  }, [focused]);

  const render = (options: readonly Completion[], label: string, emptyHint?: string) => {
    const at = target ? `${target.start}:${target.query}` : "";
    const index = active.at === at ? Math.min(active.index, options.length - 1) : 0;
    const open = target !== null && options.length > 0;
    const hint = target !== null && !open ? emptyHint : undefined;
    const pick = (chosen: T, option: Completion) => {
      const { text, caret: after } = splice(field.value, chosen, option.insert);
      edit(text);
      setCaret(after);
      // The list stays closed on the picked word as `find` reads it; a word it does not read cannot reopen it.
      const completed = find(text, chosen.start + option.insert.length);
      setClosed(completed && { start: completed.start, query: completed.query });
      placeCaret(box.current, after);
    };
    const take = (chosen: T, event: KeyboardEvent<HTMLTextAreaElement>): boolean => {
      if (event.nativeEvent.isComposing || event.shiftKey || event.altKey || event.ctrlKey || event.metaKey) return false;
      if (!options.length && event.key !== "Escape") return false;
      const step = { ArrowDown: 1, ArrowUp: -1 }[event.key];
      if (step !== undefined) {
        setActive({ at, index: (index + step + options.length) % options.length });
      } else if (event.key === "Enter" || event.key === "Tab") {
        pick(chosen, options[index]!);
      } else if (event.key === "Escape") {
        setClosed({ start: chosen.start, query: chosen.query });
        // The list's Esc, not the page's.
        event.stopPropagation();
      } else {
        return false;
      }
      event.preventDefault();
      return true;
    };
    return (
      <div className="completion-field" ref={holder}>
        <textarea
          {...field}
          ref={box}
          role={open ? "combobox" : undefined}
          aria-expanded={open || undefined}
          aria-controls={open ? listId : undefined}
          aria-autocomplete={open ? "list" : undefined}
          aria-activedescendant={open ? `${listId}-${index}` : undefined}
          onChange={(event) => {
            field.onChange(event);
            track(event.currentTarget);
          }}
          onSelect={(event) => track(event.currentTarget)}
          onScroll={place}
          // A browser may queue `select` after a caret key; the key's release says it at once.
          onKeyUp={(event) => track(event.currentTarget)}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          onKeyDown={(event) => {
            if (target && (open || hint) && take(target, event)) return;
            field.onKeyDown(event);
          }}
        />
        {hint ? <div ref={menu as RefObject<HTMLDivElement>} className="list-menu menu-list completion-list" role="status"><div className="menu-item">{hint}</div></div> : null}
        {open ? (
          <ul ref={menu as RefObject<HTMLUListElement>} className="list-menu menu-list completion-list" id={listId} role="listbox" aria-label={label}>
            {options.map((option, k) => (
              <li
                key={option.key}
                id={`${listId}-${k}`}
                role="option"
                aria-selected={k === index}
                className={k === index ? "menu-item is-active" : "menu-item"}
                title={option.title}
                // Keep the focus in the box, so typing goes on after a click.
                onMouseDown={(event) => event.preventDefault()}
                onMouseMove={() => setActive({ at, index: k })}
                onClick={() => pick(target, option)}
              >
                {option.label}
              </li>
            ))}
          </ul>
        ) : null}
      </div>
    );
  };
  return { target, render };
}

/**
 * Keep `menu` just above the caret at `index` of `box`, inside `holder`, while
 * `index` is set: on every render, a scroll of the box (the returned handler),
 * and a resize of the box or the window. It writes the menu's style directly,
 * as a scroll would otherwise re-render the field per frame.
 */
function useMenuPlace(
  box: RefObject<HTMLTextAreaElement | null>,
  holder: RefObject<HTMLDivElement | null>,
  menu: RefObject<HTMLElement | null>,
  index: number | null,
): () => void {
  // Set as each render commits, before it places the menu: a render React discards never moves it.
  const at = useRef(index);
  // One function for every render, so the observer and the window listener are set up once per opening.
  const [place] = useState(() => () => {
    const [field, around, shown] = [box.current, holder.current, menu.current];
    if (at.current === null || !field || !around || !shown) return;
    const point = caretPoint(field, at.current);
    const style: CSSProperties = {
      left: `${menuLeft({ caret: field.offsetLeft + point.left, menu: shown.offsetWidth, containerLeft: around.getBoundingClientRect().left, viewport: document.documentElement.clientWidth })}px`,
      bottom: `${around.offsetHeight - field.offsetTop - point.top + 4}px`,
    };
    Object.assign(shown.style, style);
  });
  useLayoutEffect(() => {
    at.current = index;
    place();
  });
  const showing = index !== null;
  useLayoutEffect(() => {
    if (!showing) return;
    const observer = new ResizeObserver(place);
    if (box.current) observer.observe(box.current);
    window.addEventListener("resize", place);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", place);
    };
  }, [showing, box, place]);
  return place;
}

/** `value` with `target` replaced by `insert`, a space after it at the end, and where the caret goes. */
export function splice(value: string, target: Target, insert: string): { text: string; caret: number } {
  const after = value.slice(target.end);
  const put = `${insert}${after ? "" : " "}`;
  const caret = target.start + put.length + (/^\s/.test(after) ? 1 : 0);
  return { text: value.slice(0, target.start) + put + after, caret };
}
