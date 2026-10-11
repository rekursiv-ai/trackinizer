import { useSyncExternalStore } from "react";

/** The side of the workbench Chat stands on. */
export type ChatSide = "left" | "right";

/** Where Chat stands and whether it is shown. */
export type ChatColumnState = {
  readonly side: ChatSide;
  /** Chat folded to the strip at its edge. */
  readonly hidden: boolean;
  /** Chat takes the whole main area, the workbench hidden. */
  readonly fullscreen: boolean;
};

const KEY = "trackinizer.chat.side";

/** Whoever shows the column: told of every change. */
const listeners = new Set<() => void>();

/**
 * The column as this page has it. The side is kept in the browser and read back
 * once; whether Chat is hidden or full screen is never kept, so a reload shows it.
 * Held here too when storage refuses the side, so the page still changes.
 */
let state: ChatColumnState = { side: readSide(), hidden: false, fullscreen: false };

/** Chat is hidden because the workbench stood three columns: fewer give it back. */
let hiddenByColumns = false;

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** A missing or unreadable choice puts Chat on the left. */
function readSide(): ChatSide {
  try {
    return localStorage.getItem(KEY) === "right" ? "right" : "left";
  } catch {
    return "left";
  }
}

function change(next: Partial<ChatColumnState>): void {
  const merged = { ...state, ...next };
  if (merged.side === state.side && merged.hidden === state.hidden && merged.fullscreen === state.fullscreen) return;
  state = merged;
  listeners.forEach((listener) => listener());
}

/** Stand Chat at `side` of the workbench, and keep the choice in this browser. */
export function setChatSide(side: ChatSide): void {
  try { localStorage.setItem(KEY, side); } catch { /* Private storage can be unavailable. */ }
  change({ side });
}

/** Stand Chat at the other side. */
export function flipChatSide(): void {
  setChatSide(state.side === "left" ? "right" : "left");
}

/** Fold Chat to its strip, or show it again. A folded Chat is not full screen. */
export function setChatHidden(hidden: boolean): void {
  hiddenByColumns = false;
  change(hidden ? { hidden, fullscreen: false } : { hidden });
}

/**
 * Follow the workbench's columns, told when they reach three or fall below it:
 * three take the room Chat stands in, so Chat folds to its strip, and fewer show
 * it again if the columns were what hid it. A hide or show by the user or the
 * agent since stands, and a full screen Chat is not hidden.
 */
export function followColumns(three: boolean): void {
  if (three) {
    if (state.hidden || state.fullscreen) return;
    change({ hidden: true });
    hiddenByColumns = true;
  } else if (hiddenByColumns) {
    hiddenByColumns = false;
    change({ hidden: false });
  }
}

/** Hide Chat if shown, show it if hidden. */
export function toggleChatHidden(): void {
  setChatHidden(!state.hidden);
}

/** Give Chat the whole main area, or return the workbench to it. Full screen Chat is shown. */
export function setChatFullscreen(fullscreen: boolean): void {
  change(fullscreen ? { fullscreen, hidden: false } : { fullscreen });
}

/** Chat full screen if it is not, the workbench's again if it is. */
export function toggleChatFullscreen(): void {
  setChatFullscreen(!state.fullscreen);
}

/** The column's state, kept current wherever it changes: the header's button and the shortcuts move the same column. */
export function useChatColumn(): ChatColumnState {
  return useSyncExternalStore(subscribe, currentChatColumn);
}

/** The column's state now, for what is not a component. */
export function currentChatColumn(): ChatColumnState {
  return state;
}
