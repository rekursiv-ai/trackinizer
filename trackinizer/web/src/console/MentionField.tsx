import { useLayoutEffect, useRef } from "react";
import type { FeedEvent } from "../api/sessions";
import { useCompletion } from "../composer/CompletionField";
import type { FieldProps } from "../composer/Composer";
import { useCaretPlacement } from "../composer/placeCaret";
import { addressed, type Context, mentionAt, suggest } from "./mentions";

/**
 * The console's message box (Composer's `field`): the textarea and, while the
 * caret is in a target of the line's address, a list of what `@` may complete
 * to, ranked by `suggest` over `context`, a menu at the caret. Its
 * keys and list are `useCompletion`'s; a pick puts `@name` in the target's place.
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
  const holder = useRef<HTMLDivElement>(null);
  const { target: mention, render } = useCompletion({ field, edit, find: mentionAt });
  const placeCaret = useCaretPlacement();
  useLayoutEffect(() => {
    if (!adding) return;
    const { actor, rooms = [] } = adding.line;
    const room = rooms.length > 1 ? (rooms.find((one) => context.rooms.includes(one)) ?? rooms[0]) : undefined;
    const text = addressed(field.value, room ? `${actor}:${room}` : actor);
    edit(text);
    const box = holder.current?.querySelector("textarea") ?? null;
    placeCaret(box, text.length);
    box?.focus();
    // A new click adds the agent, not a new draft: on every keystroke it would take the caret to the end.
  }, [adding]);
  const names = mention ? suggest(context, mention) : [];
  const options = names.map((name) => ({
    key: name,
    title: `@${name}`,
    insert: `@${name}`,
    label: (
      <>
        <span className="lbl">@{name}</span>
        {name === "*" ? <span className="hint">every agent shown</span> : null}
      </>
    ),
  }));
  return <div ref={holder}>{render(options, "Agents")}</div>;
}
