import {
  createContext,
  useContext,
  useEffect,
  useLayoutEffect,
  useRef,
  useSyncExternalStore,
} from "react";
import { tinykeys } from "tinykeys";

/**
 * One thing the user can do, by palette or by key.
 *
 * The one registry of these feeds both the ⌘K palette and the shortcut handler,
 * so a command's title and keys are declared once.
 */
export type Command = {
  /** Stable and unique, such as `go.activity`; a later command with the same id replaces it. */
  readonly id: string;
  readonly title: string;
  /** tinykeys bindings: `j`, `Enter`, `Space`, `g a`, `$mod+k` (⌘ on a Mac, Ctrl elsewhere). */
  readonly keys?: readonly string[];
  /** The palette's heading for it; a command without one is a shortcut only. */
  readonly section?: string;
  /** Also fires while typing in a field or inside a dialog or menu, as ⌘K does. */
  readonly global?: boolean;
  /** Fires again while its key is held, as j and k do. */
  readonly repeat?: boolean;
  readonly run: () => void;
};

/** The commands mounted now, in mounting order. */
export class CommandRegistry {
  readonly #layers = new Set<readonly Command[]>();
  readonly #listeners = new Set<() => void>();
  #commands: readonly Command[] = [];

  /** Mount `commands`; the returned function unmounts them. */
  add(commands: readonly Command[]): () => void {
    this.#layers.add(commands);
    this.#changed();
    return () => {
      this.#layers.delete(commands);
      this.#changed();
    };
  }

  /** Every mounted command, one per id; the latest mounted wins. */
  readonly getCommands = (): readonly Command[] => this.#commands;

  readonly subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  };

  #changed(): void {
    const byId = new Map<string, Command>();
    for (const layer of this.#layers) {
      for (const command of layer) {
        byId.delete(command.id);
        byId.set(command.id, command);
      }
    }
    this.#commands = [...byId.values()];
    for (const listener of this.#listeners) listener();
  }
}

export const CommandRegistryContext = createContext<CommandRegistry | null>(null);

/**
 * Mount `commands` while the calling component is mounted.
 *
 * They re-register only when an id, title or key changes, never because a
 * handler is a new closure; `run` always calls the latest one passed.
 */
export function useCommands(commands: readonly Command[]): void {
  const registry = useRegistry();
  const latest = useRef(commands);
  useLayoutEffect(() => {
    latest.current = commands;
  });
  const signature = JSON.stringify(commands.map(({ run, ...declared }) => declared));
  // Keyed on the signature, not on `commands`: callers pass a new array every
  // render, and re-registering on each would re-render every subscriber in a loop.
  useEffect(
    () =>
      registry.add(
        commands.map((command, index) => ({ ...command, run: () => latest.current[index].run() })),
      ),
    [registry, signature],
  );
}

/** Every mounted command, for the palette. */
export function useCommandList(): readonly Command[] {
  const registry = useRegistry();
  return useSyncExternalStore(registry.subscribe, registry.getCommands);
}

/** Bind every mounted command's keys on the window. */
export function Shortcuts() {
  const commands = useCommandList();
  useEffect(() => bindShortcuts(window, commands), [commands]);
  return null;
}

/**
 * Bind `commands`' keys on `target`; the returned function unbinds them.
 *
 * Keys pressed where an element handles them itself are left to it: typing in a
 * field, anything inside a dialog or menu (so a digit typed in a menu's search
 * box types the digit, COLD-15), the keys a listbox takes (it prevents their
 * default), and Enter or Space on a button or link. Only `global` commands fire
 * there. A later command bound to the same key wins, global or not: one listener
 * holds every binding, latest first, and tinykeys runs only the first that
 * matches a key, so two can never both run.
 */
export function bindShortcuts(target: Window, commands: readonly Command[]): () => void {
  const bindings: { [key: string]: (event: KeyboardEvent) => void } = {};
  for (const command of commands.toReversed()) {
    for (const key of command.keys ?? []) {
      bindings[key] ??= (event) => {
        if ((event.repeat && !command.repeat) || (!command.global && handlesOwnKeys(event))) return;
        event.preventDefault();
        command.run();
      };
    }
  }
  return tinykeys(target, bindings, { ignore: (event) => event.isComposing });
}

/**
 * A tinykeys binding as the key caps this platform shows: `g a` → G, A;
 * `$mod+k` → ⌘K on a Mac and Ctrl+K elsewhere, as tinykeys binds `$mod`;
 * `$mod+Enter` → ⌘↵ and Ctrl+Enter.
 */
export function keyCaps(binding: string): string[] {
  const mac = /Mac|iPod|iPhone|iPad/.test(navigator.platform);
  return binding.split(" ").map((chord) =>
    chord
      .split("+")
      .map((part) =>
        part === "$mod" ? (mac ? "⌘" : "Ctrl") : part === "Enter" && mac ? "↵" : part.length === 1 ? part.toUpperCase() : part,
      )
      .join(mac ? "" : "+"),
  );
}

function useRegistry(): CommandRegistry {
  const registry = useContext(CommandRegistryContext);
  if (!registry) throw new Error("Commands need a CommandRegistryContext above them.");
  return registry;
}

function handlesOwnKeys(event: KeyboardEvent): boolean {
  const target = event.target;
  if (!(target instanceof Element)) return false;
  if (target.closest(TYPING)) return true;
  // React has run the listbox's own handler by now, at the app's root, before the
  // key reached the window. Leaving a listbox every key would strand the view's
  // there: Esc, `.` and `/` in the graph's roots list.
  if (target.closest('[role="listbox"]')) return event.defaultPrevented;
  return (event.key === "Enter" || event.key === " ") && target.closest(ACTIVATES) !== null;
}

const TYPING =
  'input, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="dialog"], [role="alertdialog"], [role="menu"]';
const ACTIVATES = 'a[href], button, summary, [role="button"], [role="link"], [role="tab"], [role="checkbox"]';
