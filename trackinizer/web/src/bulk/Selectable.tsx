import type { ReactElement } from "react";
import { Icon } from "../ui/icons";
import "./bulk.css";

/**
 * A list row, `children`, with the mock's check before it: a click toggles the
 * row's selection, and a shift-click selects the rows between the one clicked
 * last and this one. The check shows on hover, on the focused row, and on every
 * row once any is selected.
 *
 * A sibling of the row's link, not inside it: a button inside a link is invalid
 * markup, and assistive technology would read the two as one control.
 */
export function Selectable({
  id,
  name,
  selected,
  onSelect,
  children,
}: {
  id: string;
  /** The row as a link names it: `Issue#12`. */
  name: string;
  selected: boolean;
  onSelect: (id: string, range: boolean) => void;
  children: ReactElement;
}) {
  return (
    <div className={selected ? "row-line is-selected" : "row-line"}>
      <button
        type="button"
        className="row-check"
        aria-pressed={selected}
        aria-label={`Select ${name}`}
        onClick={(event) => onSelect(id, event.shiftKey)}
      >
        {selected ? <Icon name="check" size={12} /> : null}
      </button>
      {children}
    </div>
  );
}
