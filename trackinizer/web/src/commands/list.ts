import type { Command } from "./registry";

/** What a list does for each of its keys. */
export type ListHandlers = {
  readonly next: () => void;
  readonly previous: () => void;
  readonly open: () => void;
  readonly close: () => void;
  readonly peek: () => void;
};

/**
 * The list keys (j, k, Enter, Esc, Space) as commands, wired to `handlers`.
 *
 * A list view mounts them with `useCommands(listCommands({...}))`, so the keys
 * work only while a list is on screen.
 */
export function listCommands(handlers: ListHandlers): Command[] {
  return [
    { id: "list.next", title: "Next row", keys: ["j", "ArrowDown"], repeat: true, run: handlers.next },
    { id: "list.previous", title: "Previous row", keys: ["k", "ArrowUp"], repeat: true, run: handlers.previous },
    { id: "list.open", title: "Open row", keys: ["Enter", "o"], run: handlers.open },
    { id: "list.close", title: "Close", keys: ["Escape"], run: handlers.close },
    { id: "list.peek", title: "Peek", keys: ["Space"], run: handlers.peek },
  ];
}
