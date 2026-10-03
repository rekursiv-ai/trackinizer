import { type KeyboardEvent, useId, useLayoutEffect, useRef, useState } from "react";
import type { FeedEvent } from "../api/sessions";
import type { FieldProps } from "../composer/Composer";
import { addressed, complete, type Context, type Mention, mentionAt, suggest } from "./mentions";
import "../lists/lists.css";
import "./MentionField.css";

/**
 * The console's message box (Composer's `field`): the textarea and, while the
 * caret is in a target of the line's address, a list of what `@` may complete
 * to, ranked by `suggest` over `context`, a menu over the box's left edge.
 *
 * As GitHub's comment box does, the textarea is a combobox only while the list
 * shows, naming the active option (`aria-activedescendant`) and keeping the
 * focus. ↑ and ↓ move, wrapping; Enter or Tab takes the active option, and a
 * click any; Esc closes the list until the caret is in another target or
 * before where it closed. While an input method composes, its keys are its
 * own. With the list closed, every key is the composer's, so Enter sends.
 *
 * `adding` is a line whose agent was clicked: the line's address takes the
 * agent (`addressed`), as `agent:room` in the line's room when it is in several,
 * one the view shows first, as `suggest` offers it; the box takes the focus,
 * the caret at the end.
 */
export function MentionField({
  field,
  edit,
  context,
  adding = null,
}: {
  field: FieldProps;
  edit: (text: string) => void;
  context: Context;
  adding?: { readonly line: FeedEvent } | null;
}) {
  const listId = useId();
  const box = useRef<HTMLTextAreaElement>(null);
  const [caret, setCaret] = useState(0);
  const [focused, setFocused] = useState(false);
  // The target Esc or a pick closed the list on, and its query then.
  const [closed, setClosed] = useState<{ readonly start: number; readonly query: string } | null>(null);
  const [active, setActive] = useState({ at: "", index: 0 });
  // Where the caret goes once a picked target is in the box, whose new value puts it at the end.
  const placing = useRef<number | null>(null);
  useLayoutEffect(() => {
    if (placing.current === null) return;
    box.current?.setSelectionRange(placing.current, placing.current);
    placing.current = null;
  });
  useLayoutEffect(() => {
    if (!adding) return;
    const { actor, rooms = [] } = adding.line;
    const room = rooms.length > 1 ? (rooms.find((one) => context.rooms.includes(one)) ?? rooms[0]) : undefined;
    const text = addressed(field.value, room ? `${actor}:${room}` : actor);
    edit(text);
    placing.current = text.length;
    box.current?.focus();
    // A new click adds the agent, not a new draft: on every keystroke it would take the caret to the end.
  }, [adding]);
  const mention = mentionAt(field.value, caret);
  const shut = !mention || !focused || field.disabled || (closed?.start === mention.start && mention.query.startsWith(closed.query));
  const names = mention && !shut ? suggest(context, mention) : [];
  const at = mention ? `${mention.start}:${mention.query}` : "";
  const index = active.at === at ? Math.min(active.index, names.length - 1) : 0;

  /** Follow the caret; out of every target, the list may open again where it was closed. */
  const track = ({ value, selectionStart }: HTMLTextAreaElement) => {
    setCaret(selectionStart);
    if (!mentionAt(value, selectionStart)) setClosed(null);
  };
  const pick = (target: Mention, name: string) => {
    const { text, caret: after } = complete(field.value, target, name);
    edit(text);
    setCaret(after);
    setClosed({ start: target.start, query: name });
    placing.current = after;
  };
  /** Whether the open list took `event`'s key. */
  const take = (target: Mention, event: KeyboardEvent<HTMLTextAreaElement>): boolean => {
    const step = { ArrowDown: 1, ArrowUp: -1 }[event.key];
    if (step !== undefined) {
      setActive({ at, index: (index + step + names.length) % names.length });
    } else if ((event.key === "Enter" || event.key === "Tab") && !event.shiftKey) {
      pick(target, names[index]!);
    } else if (event.key === "Escape") {
      setClosed({ start: target.start, query: target.query });
      // The list's Esc, not the page's.
      event.stopPropagation();
    } else {
      return false;
    }
    event.preventDefault();
    return true;
  };

  const open = mention !== null && names.length > 0;
  return (
    <div className="mention-field">
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
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        onKeyDown={(event) => {
          if (open && !event.nativeEvent.isComposing && take(mention, event)) return;
          field.onKeyDown(event);
        }}
      />
      {open ? (
        <ul className="list-menu menu-list mention-list" id={listId} role="listbox" aria-label="Agents">
          {names.map((name, k) => (
            <li
              key={name}
              id={`${listId}-${k}`}
              role="option"
              aria-selected={k === index}
              className={k === index ? "menu-item is-active" : "menu-item"}
              title={`@${name}`}
              // Keep the focus in the box, so typing goes on after a click.
              onMouseDown={(event) => event.preventDefault()}
              onMouseMove={() => setActive({ at, index: k })}
              onClick={() => pick(mention, name)}
            >
              <span className="lbl">@{name}</span>
              {name === "*" ? <span className="hint">every agent shown</span> : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
