import { useQueryClient } from "@tanstack/react-query";
import { type ReactElement, useRef, useState } from "react";
import { useProfile } from "../app/boot";
import { loadedValues } from "../editors/values";
import { Menu, type MenuOption } from "../lists/Menu";
import { useBrowserState } from "../state/store";
import { Avatar } from "../ui/glyphs";
import { Icon } from "../ui/icons";
import { PersonDialog } from "./PersonDialog";
import { draftFor, offered, type PersonDraft } from "./people";
import "./people.css";

/**
 * The one picker for owners and subscribers, wherever they are set: a detail's
 * properties, a create form's chips and the bulk bar. It offers me, the values
 * set now, everyone on the rows already loaded (no route lists people), and the
 * people and agents added by hand in this browser, each by name.
 *
 * Below them, "New person…", "New agent…", and "Add “…”…" for typed text open
 * the dialog that adds one (`PersonDialog`), then pick them. A pick is the value
 * written (an email, a name, a handle), never the name shown; "me" is the
 * account email.
 *
 * `field` says which: an owner is one value, or none through the `none` row;
 * subscribers are several, each pick a toggle. `checked` says what is set now,
 * asked with `null` for none; `current` holds values to offer whether or not a
 * loaded row has them. `onPick` gets what was picked, `null` for none. One added
 * through the dialog reaches the `onPick` the picker had when the dialog opened,
 * so it is guarded against what the user saw then, and is never toggled off.
 */
export function PeoplePicker({
  field,
  trigger,
  current,
  checked,
  none,
  onPick,
}: {
  field: "owner" | "subscribers";
  /** The button that opens it. */
  trigger: ReactElement;
  current: readonly string[];
  checked: (actor: string | null) => boolean;
  /** The label of the row that sets no owner: `No owner`. */
  none?: string;
  onPick: (actor: string | null) => void;
}) {
  const queryClient = useQueryClient();
  const { email } = useProfile();
  const [{ people }] = useBrowserState();
  const [adding, setAdding] = useState<{ readonly draft: PersonDraft; readonly add: (actor: string) => void } | null>(null);
  const anchor = useRef<HTMLSpanElement>(null);
  const many = field === "subscribers";
  const actors = [...current, ...loadedValues(queryClient, "owner"), ...loadedValues(queryClient, "subscribers"), ...Object.keys(people)];
  const options: MenuOption[] = offered(email, actors, people).map(({ actor, label, hint }) => ({
    value: actor,
    label,
    hint,
    icon: <Avatar actor={actor} size={16} />,
    checked: checked(actor),
  }));
  if (none !== undefined) options.push({ value: NONE, label: none, icon: <Icon name="user" size={14} />, checked: checked(null) });
  const fixed = (typed: string): MenuOption[] => {
    const needle = typed.toLowerCase();
    const known = options.some((option) => option.value.toLowerCase() === needle || option.label.toLowerCase() === needle);
    return [
      ...(typed && !known ? [{ value: `${ADD}${typed}`, label: `Add “${typed}”…`, icon: <Icon name="plus" size={14} /> }] : []),
      { value: NEW_PERSON, label: "New person…", icon: <Icon name="user" size={14} /> },
      { value: NEW_AGENT, label: "New agent…", icon: <Icon name="bot" size={14} /> },
    ];
  };
  const pick = (value: string) => {
    if (value === NONE) return onPick(null);
    const draft =
      value === NEW_PERSON
        ? draftFor("", "person")
        : value === NEW_AGENT
          ? draftFor("", "agent")
          : value.startsWith(ADD)
            ? draftFor(value.slice(ADD.length))
            : null;
    if (!draft) return onPick(value);
    setAdding({
      draft,
      add: (actor) => {
        if (!many || !checked(actor)) onPick(actor);
      },
    });
  };
  return (
    <>
      <span className="np-anchor" ref={anchor}>
        <Menu
          label={many ? "Add subscribers: people or agents…" : "Assign owner: a person or an agent…"}
          multi={many}
          trigger={trigger}
          options={options}
          onPick={pick}
          fixed={fixed}
        />
      </span>
      {adding ? (
        <PersonDialog
          role={many ? "subscriber" : "owner"}
          draft={adding.draft}
          returnFocus={() => anchor.current?.querySelector("button")?.focus()}
          onClose={() => setAdding(null)}
          onAdd={(actor) => {
            setAdding(null);
            adding.add(actor);
          }}
        />
      ) : null}
    </>
  );
}

// Menu values of the rows that are not people; no actor's value starts with a NUL.
const NONE = "\u0000none";
const NEW_PERSON = "\u0000person";
const NEW_AGENT = "\u0000agent";
const ADD = "\u0000add:";
