import { useCallback, useLayoutEffect, useRef, useState } from "react";
import { useWriteMode } from "../app/boot";
import { useCommands } from "../commands/registry";

/** A list's selected rows, for edits to all of them at once. */
export type Selection = {
  /** Off for a viewer, who has nothing to apply to a selection. */
  readonly enabled: boolean;
  readonly ids: ReadonlySet<string>;
  /** A click on row `id`: toggle it, or with `range`, select from the row clicked last to it. Stable. */
  readonly select: (id: string, range: boolean) => void;
  readonly clear: () => void;
};

/**
 * Select rows of a list, as the mock does: a row's check toggles it, x toggles
 * the focused row, and a shift-click selects every row shown between the row
 * clicked last and this one. `order` is the rows' ids as shown.
 */
export function useSelection(order: readonly string[], focused: string | null): Selection {
  const enabled = useWriteMode() !== "hidden";
  const [ids, setIds] = useState<ReadonlySet<string>>(NONE);
  const anchor = useRef<string | null>(null);
  const shown = useRef(order);
  useLayoutEffect(() => {
    shown.current = order;
  });
  const select = useCallback((id: string, range: boolean) => {
    const from = range ? anchor.current : null;
    setIds((selected) => clickSelect(selected, shown.current, from, id));
    anchor.current = id;
  }, []);
  const clear = useCallback(() => {
    setIds(NONE);
    anchor.current = null;
  }, []);
  useCommands(
    enabled
      ? [
          {
            id: "list.select",
            title: "Select row",
            keys: ["x"],
            run: () => {
              if (focused) select(focused, false);
            },
          },
        ]
      : [],
  );
  return { enabled, ids, select, clear };
}

/**
 * `selected` after a click on row `id`. Without an `anchor`, the click toggles
 * `id`. With one, it selects every row of `order` from `anchor` to `id`, both
 * included, and leaves the rest as they were; an anchor no longer shown makes
 * it a plain toggle.
 */
export function clickSelect(
  selected: ReadonlySet<string>,
  order: readonly string[],
  anchor: string | null,
  id: string,
): ReadonlySet<string> {
  const [from, to] = [anchor === null ? -1 : order.indexOf(anchor), order.indexOf(id)];
  if (from < 0 || to < 0) {
    const next = new Set(selected);
    if (!next.delete(id)) next.add(id);
    return next;
  }
  return new Set([...selected, ...order.slice(Math.min(from, to), Math.max(from, to) + 1)]);
}

const NONE: ReadonlySet<string> = new Set();
