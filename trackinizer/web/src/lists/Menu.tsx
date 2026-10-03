import { Popover } from "radix-ui";
import { type KeyboardEvent, type ReactElement, type ReactNode, useEffect, useId, useState } from "react";
import { Icon } from "../ui/icons";

/** One choice in a menu. */
export type MenuOption = {
  readonly value: string;
  readonly label: string;
  readonly icon?: ReactNode;
  /** Shown on the right, muted. */
  readonly hint?: string;
  /** Ticked: the current grouping, or a filter value in use. */
  readonly checked?: boolean;
  /** Destructive, such as Purge: drawn in the error colour. */
  readonly danger?: boolean;
};

/**
 * The mock's menu: a popover with a search box over a list of options.
 *
 * Typing narrows the options, to those whose label or value holds the text
 * unless `match` says otherwise; arrow keys move, Enter picks the active option,
 * which is the first match until the arrows move it (the typed value, when
 * offered, comes after the matches, and `fixed` rows last), and Escape closes and returns focus to the trigger. The input keeps focus throughout, with the
 * active option named by `aria-activedescendant`. There are no digit shortcuts,
 * so a digit typed in the search box is a digit (COLD-15).
 */
export function Menu({
  label,
  trigger,
  options,
  onPick,
  multi = false,
  create,
  fixed,
  match,
  onBack,
  onClose,
}: {
  /** The search box's placeholder and the menu's accessible name: `Group by…`. */
  label: string;
  /** The button that opens it. */
  trigger: ReactElement;
  options: readonly MenuOption[];
  onPick: (value: string) => void;
  /** Stay open after a pick, to tick several. */
  multi?: boolean;
  /** Offer what was typed as a value too, labelled `create(typed)`. */
  create?: (typed: string) => string;
  /**
   * Rows below a rule, offered whatever is typed and given it: "New person…".
   * Each opens something of its own, so picking one closes the menu, `multi` or not.
   */
  fixed?: (typed: string) => readonly MenuOption[];
  /** Whether `option` answers the typed text, trimmed; it may be empty. */
  match?: (option: MenuOption, typed: string) => boolean;
  /** Backspace in an empty search box: go back a step. */
  onBack?: () => void;
  onClose?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const change = (next: boolean) => {
    setOpen(next);
    if (!next) onClose?.();
  };
  return (
    <Popover.Root open={open} onOpenChange={change}>
      <Popover.Trigger asChild>{trigger}</Popover.Trigger>
      <Popover.Portal>
        <Popover.Content className="list-menu" align="start" sideOffset={4} aria-label={label}>
          <MenuBody
            // A new step starts with an empty search box and the first option active.
            key={label}
            label={label}
            options={options}
            onPick={(value, closing) => {
              onPick(value);
              if (!multi || closing) change(false);
            }}
            create={create}
            fixed={fixed}
            match={match}
            onBack={onBack}
          />
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

function MenuBody({
  label,
  options,
  onPick,
  create,
  fixed,
  match,
  onBack,
}: {
  label: string;
  options: readonly MenuOption[];
  /** `closing` for a fixed row, which closes even a `multi` menu. */
  onPick: (value: string, closing: boolean) => void;
  create?: (typed: string) => string;
  fixed?: (typed: string) => readonly MenuOption[];
  match?: (option: MenuOption, typed: string) => boolean;
  onBack?: () => void;
}) {
  const id = useId();
  const [text, setText] = useState("");
  const [active, setActive] = useState(0);
  const typed = text.trim();
  const needle = typed.toLowerCase();
  const shown = options.filter((option) =>
    match ? match(option, typed) : option.label.toLowerCase().includes(needle) || option.value.toLowerCase().includes(needle),
  );
  const created: MenuOption | null =
    create && typed && !options.some((option) => option.value === typed)
      ? { value: typed, label: create(typed), icon: <Icon name="plus" size={14} /> }
      : null;
  const below = fixed?.(typed) ?? [];
  const offered = [...shown, ...(created ? [created] : []), ...below];
  const firstFixed = offered.length - below.length;
  const current = Math.min(active, offered.length - 1);
  const optionId = (index: number) => `${id}-${index}`;
  useEffect(() => {
    document.getElementById(`${id}-${current}`)?.scrollIntoView({ block: "nearest" });
  }, [id, current]);

  const pick = (index: number) => {
    const option = offered[index];
    if (!option) return;
    onPick(option.value, index >= firstFixed);
    if (option === created) setText("");
  };
  const onKeyDown = (event: KeyboardEvent) => {
    // While an input method composes, Enter and Backspace are its own: they
    // commit or edit the text being composed, not pick or go back.
    if (event.nativeEvent.isComposing) return;
    const step = { ArrowDown: 1, ArrowUp: -1 }[event.key];
    if (step !== undefined && offered.length) {
      setActive((current + step + offered.length) % offered.length);
    } else if (event.key === "Enter") {
      pick(current);
    } else if (event.key === "Backspace" && text === "" && onBack) {
      onBack();
    } else {
      return;
    }
    event.preventDefault();
  };

  return (
    <>
      <div className="menu-search">
        <Icon name="search" size={14} />
        <input
          autoFocus
          role="combobox"
          aria-label={label}
          aria-expanded="true"
          aria-controls={`${id}-list`}
          aria-activedescendant={offered.length ? optionId(current) : undefined}
          aria-autocomplete="list"
          placeholder={label}
          autoComplete="off"
          value={text}
          onChange={(event) => {
            setText(event.target.value);
            setActive(0);
          }}
          onKeyDown={onKeyDown}
        />
      </div>
      <div className="menu-list" role="listbox" id={`${id}-list`} aria-label={label}>
        {offered.flatMap((option, index) => [
          ...(index === firstFixed && index > 0 ? [<div key={SEPARATOR} className="menu-sep" aria-hidden="true" />] : []),
          <div
            key={option.value}
            id={optionId(index)}
            role="option"
            aria-selected={option.checked ?? false}
            className={["menu-item", index === current && "is-active", option.danger && "danger"].filter(Boolean).join(" ")}
            // Keep focus in the search box, so typing goes on working after a click.
            onMouseDown={(event) => event.preventDefault()}
            onMouseMove={() => setActive(index)}
            onClick={() => pick(index)}
          >
            {option.icon && (
              <span className="menu-icon" aria-hidden="true">
                {option.icon}
              </span>
            )}
            <span className="lbl">{option.label}</span>
            {option.hint && <span className="hint">{option.hint}</span>}
            {option.checked && <Icon name="check" size={14} className="chk" />}
          </div>,
        ])}
        {offered.length === 0 && <div className="menu-empty">No matches</div>}
      </div>
    </>
  );
}

/** The key of the rule above the fixed rows, which no option's value may be. */
const SEPARATOR = "\u0000separator";
