import { useSyncExternalStore } from "react";
import { choosePreferences, preferences, subscribePreferences } from "../preferences";

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

/** Whoever shows the column: told of every change. */
const listeners = new Set<() => void>();

/**
 * The column as this page has it. The side is one of the user's preferences, and
 * follows them; whether Chat is hidden or full screen is this tab's alone and never
 * kept, so a reload shows it.
 */
let state: ChatColumnState = { side: preferences().chat_side, hidden: false, fullscreen: false };

/** Chat is hidden because the workbench stood three columns: fewer give it back. */
let hiddenByColumns = false;

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function change(next: Partial<ChatColumnState>): void {
  const merged = { ...state, ...next };
  if (merged.side === state.side && merged.hidden === state.hidden && merged.fullscreen === state.fullscreen) return;
  state = merged;
  listeners.forEach((listener) => listener());
}

/** Stand Chat at `side` of the workbench, and keep the choice in the user's preferences. */
export function setChatSide(side: ChatSide): void {
  change({ side });
  void choosePreferences({ chat_side: side });
}

// The side follows the preference wherever it changes: here, the server, or the agent.
subscribePreferences(() => change({ side: preferences().chat_side }));

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

/** What a phone shows, one at a time: Chat or the canvas. */
export type PhoneView = "chat" | "canvas";

/** This tab's alone and never kept, so a reload shows Chat. */
let phoneView: PhoneView = "chat";

/** Show Chat or the canvas on a phone. */
export function setPhoneView(view: PhoneView): void {
  if (view === phoneView) return;
  phoneView = view;
  listeners.forEach((listener) => listener());
}

/** What a phone shows now, kept current wherever it changes. */
export function usePhoneView(): PhoneView {
  return useSyncExternalStore(subscribe, () => phoneView);
}
