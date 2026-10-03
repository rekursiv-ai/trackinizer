import { Popover } from "radix-ui";
import { type ReactElement, useRef, useState } from "react";
import { Menu, type MenuOption } from "../lists/Menu";
import { PRIORITY_NAMES, PriorityGlyph, priorityBand, priorityName } from "../ui/glyphs";

/**
 * Pick an Issue priority: one of the four named bands, No priority, or an exact
 * number, which opens its own small form. It only reports the pick, so a create
 * form's draft uses it as the detail does (COLD-10).
 *
 * `value` is the current priority, `undefined` when unset; `mixed` says the rows
 * it sets have different ones, so none is ticked. `onPick` gets the new one,
 * `null` for none. A band sets its round number (P1 is 10), so any other
 * integer comes from the exact form.
 */
export function PriorityPicker({
  value,
  mixed = false,
  onPick,
  trigger,
}: {
  value: number | undefined;
  mixed?: boolean;
  onPick: (value: number | null) => void;
  trigger: ReactElement;
}) {
  const [exact, setExact] = useState(false);
  const anchor = useRef<HTMLSpanElement>(null);
  const band = value === undefined ? null : priorityBand(value);
  const options: MenuOption[] = [
    ...PRIORITY_NAMES.map((name, index) => ({
      value: String(index * 10),
      label: name,
      icon: <PriorityGlyph priority={index * 10} />,
      checked: !mixed && band === index,
    })),
    { value: NONE, label: "No priority", icon: <PriorityGlyph priority={null} />, checked: !mixed && value === undefined },
    { value: EXACT, label: "Exact number…", hint: value === undefined ? "integer" : String(value) },
  ];
  const pick = (picked: string) => {
    if (picked === EXACT) setExact(true);
    else onPick(picked === NONE ? null : Number(picked));
  };
  return (
    // Modal, so its focus trap keeps the input focused: the menu that opens it
    // hands focus back to its trigger a moment after it closes.
    <Popover.Root open={exact} onOpenChange={setExact} modal>
      <Popover.Anchor asChild>
        <span className="ed-anchor" ref={anchor}>
          <Menu label="Set priority…" trigger={trigger} options={options} onPick={pick} />
        </span>
      </Popover.Anchor>
      <Popover.Portal>
        <Popover.Content
          className="list-menu popover"
          align="start"
          sideOffset={4}
          aria-label="Exact priority"
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            anchor.current?.querySelector("button")?.focus();
          }}
        >
          <ExactPriority
            value={value}
            onSave={(picked) => {
              setExact(false);
              onPick(picked);
            }}
          />
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

/** A priority as text: `P1 High (10)`, or `No priority`. */
export function priorityText(value: unknown): string {
  return typeof value === "number" ? `${priorityName(value)} (${value})` : "No priority";
}

/**
 * The exact number: any integer the server accepts. Empty means no priority.
 * The server checks the rest (a whole number, at least 0) and says what is wrong.
 */
function ExactPriority({ value, onSave }: { value: number | undefined; onSave: (value: number | null) => void }) {
  const [text, setText] = useState(value === undefined ? "" : String(value));
  return (
    <form
      className="ed-form"
      onSubmit={(event) => {
        event.preventDefault();
        onSave(text.trim() === "" ? null : Number(text));
      }}
    >
      <h5>Priority</h5>
      <input
        className="field num"
        type="number"
        step={1}
        min={0}
        aria-label="Exact priority"
        value={text}
        onChange={(event) => setText(event.target.value)}
      />
      <div className="pop-row">
        <span className="muted">Lower is more urgent; each band spans ten.</span>
        <button type="submit" className="btn primary">
          Save
        </button>
      </div>
    </form>
  );
}

const NONE = "\u0000none";
const EXACT = "\u0000exact";
