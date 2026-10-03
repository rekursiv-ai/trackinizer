import { useQueryClient } from "@tanstack/react-query";
import type { ReactElement, ReactNode } from "react";
import { useMeta } from "../app/boot";
import { PriorityPicker } from "../editors/Priority";
import { asList, loadedValues } from "../editors/values";
import { Menu, type MenuOption } from "../lists/Menu";
import { actorName } from "../people/people";
import { PeoplePicker } from "../people/Picker";
import { useBrowserState } from "../state/store";
import { Avatar, capitalize, JudgementGlyph, LabelDot, PriorityGlyph, priorityName, StatusGlyph } from "../ui/glyphs";
import { Icon } from "../ui/icons";

/**
 * The draft's chips under its text, as in the mock: judgement, status, priority,
 * type, owner, subscribers and labels, each where the kind has the field. Each
 * opens the picker the detail uses for that field, and only sets the draft.
 *
 * `chips` holds a value per chip the kind has; `onChange` sets one. `disabled`
 * turns every chip off.
 */
export function Chips({
  chips,
  disabled,
  onChange,
}: {
  chips: { readonly [field: string]: unknown };
  disabled: boolean;
  onChange: (field: string, value: unknown) => void;
}) {
  const queryClient = useQueryClient();
  const { enums } = useMeta();
  const [{ people }] = useBrowserState();

  /** A chip that sets one value, or none through its `unset` option. */
  const one = (field: string, name: string, values: readonly string[], look: (value: string) => Look, unset?: Look & { label: string }) => {
    const value = typeof chips[field] === "string" ? (chips[field] as string) : null;
    const shown = value === null ? unset! : look(value);
    const options: MenuOption[] = [...new Set([...values, ...(value === null ? [] : [value])])].map((option) => ({
      value: option,
      label: look(option).text,
      ...look(option),
      checked: option === value,
    }));
    if (unset) options.push({ value: CLEAR, label: unset.label, icon: unset.icon, checked: value === null });
    return (
      <Menu
        key={field}
        label={`${name}…`}
        trigger={chip(field, name, shown, disabled)}
        options={options}
        onPick={(picked) => onChange(field, picked === CLEAR ? null : picked)}
      />
    );
  };
  /** A chip that ticks several values, each a toggle. */
  const many = (field: string, name: string, values: readonly string[], look: (values: readonly string[]) => Look, icon?: (value: string) => Partial<Look>, create?: (typed: string) => string) => {
    const shown = asList(chips[field]);
    return (
      <Menu
        key={field}
        label={`${name}…`}
        multi
        trigger={chip(field, name, look(shown), disabled)}
        options={[...new Set([...values, ...shown])].map((value) => ({ value, label: value, ...icon?.(value), checked: shown.includes(value) }))}
        onPick={(value) => onChange(field, shown.includes(value) ? shown.filter((item) => item !== value) : [...shown, value])}
        create={create}
      />
    );
  };

  const priority = typeof chips.priority === "number" ? chips.priority : null;
  const shownPriority = { icon: <PriorityGlyph priority={priority} />, text: priorityName(priority) };
  const pieces: { readonly [field: string]: () => ReactElement } = {
    judgement: () =>
      one("judgement", "Judgement", enums.judgement ?? [], (value) => ({ icon: <JudgementGlyph judgement={value} />, text: capitalize(value) }), {
        label: "No judgement",
        text: "No judgement",
      }),
    status: () => one("status", "Status", enums.status ?? [], (value) => ({ icon: <StatusGlyph status={value} />, text: capitalize(value) })),
    priority: () => (
      <PriorityPicker
        key="priority"
        value={priority ?? undefined}
        onPick={(value) => onChange("priority", value)}
        trigger={chip(
          "priority",
          "Priority",
          priority === null || priority % 10 === 0 ? shownPriority : { ...shownPriority, text: `${shownPriority.text} (${priority})` },
          disabled,
        )}
      />
    ),
    issue_kind: () => many("issue_kind", "Type", enums.issue_kind ?? [], (types) => ({ text: types.length ? types.join(", ") : "No type" })),
    // The people picker offers me, loaded people and those added by hand; a chip names them by name.
    owner: () => {
      const owner = typeof chips.owner === "string" ? chips.owner : null;
      return (
        <PeoplePicker
          key="owner"
          field="owner"
          trigger={chip(
            "owner",
            "Owner",
            owner === null
              ? { icon: <Icon name="user" size={13} />, text: "No owner" }
              : { icon: <Avatar actor={owner} size={16} />, text: actorName(owner, people) },
            disabled,
          )}
          current={owner === null ? [] : [owner]}
          checked={(actor) => actor === owner}
          none="No owner"
          onPick={(actor) => onChange("owner", actor)}
        />
      );
    },
    subscribers: () => {
      const actors = asList(chips.subscribers);
      return (
        <PeoplePicker
          key="subscribers"
          field="subscribers"
          trigger={chip(
            "subscribers",
            "Subscribers",
            actors.length
              ? {
                  icon: <Avatar actor={actors[0]!} size={16} />,
                  text: actors.length === 1 ? actorName(actors[0]!, people) : `${actors.length} subscribers`,
                }
              : { icon: <Icon name="bell" size={13} />, text: "No subscribers" },
            disabled,
          )}
          current={actors}
          checked={(actor) => actor !== null && actors.includes(actor)}
          onPick={(actor) => onChange("subscribers", actors.includes(actor!) ? actors.filter((item) => item !== actor) : [...actors, actor!])}
        />
      );
    },
    labels: () =>
      many(
        "labels",
        "Labels",
        loadedValues(queryClient, "labels"),
        (labels) => (labels.length ? { icon: <LabelDot label={labels[0]!} />, text: labels.join(", ") } : { icon: <Icon name="tag" size={13} />, text: "No labels" }),
        (label) => ({ icon: <LabelDot label={label} /> }),
        (typed) => `Create label “${typed}”`,
      ),
  };
  return <div className="cr-chips">{Object.keys(chips).map((field) => pieces[field]?.())}</div>;
}

/** How a chip or a menu option shows a value: an icon and its text. */
type Look = { readonly icon?: ReactNode; readonly text: string };

/**
 * A chip: the button that opens its field's picker. Its name says the field and
 * the value, `Status: Active`, since the value alone does not say which chip it is.
 */
function chip(field: string, name: string, { icon, text }: Look, disabled: boolean): ReactElement {
  return (
    <button
      type="button"
      className="chip-btn"
      data-field={field}
      aria-label={`${name}: ${text}`}
      title={`${name}: ${text}`}
      disabled={disabled}
    >
      {icon}
      <span className="cr-chip-text">{text}</span>
    </button>
  );
}

/** The menu value that unsets a chip; no stored value is a NUL. */
const CLEAR = "\u0000clear";
