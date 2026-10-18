import type { KeyboardEvent } from "react";
import type { Bounds } from "./markGeometry";

/**
 * One red box: an outline at `bounds` (fractions of whatever it stands in, the stage
 * or a tile's body), in percentages so that CSS alone moves and sizes it. With
 * `onRemove`, the outline takes a click and the keyboard; without, it is a draft.
 */
export function MarkBox({ bounds, id, author, onRemove }: {
  readonly bounds: Bounds;
  readonly id?: string;
  readonly author?: string;
  readonly onRemove?: () => void;
}) {
  const percent = (fraction: number) => `${Math.round(fraction * 1_000_000) / 10_000}%`;
  const style = {
    left: percent((bounds.left + bounds.right) / 2), top: percent((bounds.top + bounds.bottom) / 2),
    width: percent(bounds.right - bounds.left), height: percent(bounds.bottom - bounds.top),
  };
  const removeByKey = (event: KeyboardEvent) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    onRemove?.();
  };
  return (
    <svg className={id ? "mark-box" : "mark-draft"} data-mark={id} data-author={author} style={style}>
      <rect width="100%" height="100%" className="mark-outline" />
      {onRemove && <rect width="100%" height="100%" className="mark-hit" role="button" tabIndex={0} aria-label="Remove box"
        onClick={onRemove} onKeyDown={removeByKey}>
        <title>Click to remove this box</title>
      </rect>}
    </svg>
  );
}
