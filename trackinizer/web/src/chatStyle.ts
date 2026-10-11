import { useSyncExternalStore } from "react";

export type ChatStyle = "default" | "terminal";

const KEY = "trackinizer.chat.style";

/** Whoever shows the style: told of every change. */
const listeners = new Set<() => void>();

/**
 * The style chosen on this page, which holds whether or not storage kept it:
 * read back from a storage that refused it (a private window, a full quota),
 * Settings would show the old one.
 */
let chosen: ChatStyle | null = null;

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** A missing or unreadable choice preserves today's look. */
export function readChatStyle(): ChatStyle {
  try {
    return localStorage.getItem(KEY) === "terminal" ? "terminal" : "default";
  } catch {
    return "default";
  }
}

/** Mark the page with the style, which the Chat panel's stylesheet selects on; Default leaves it unmarked. */
export function applyChatStyle(choice: ChatStyle): void {
  if (choice === "terminal") document.documentElement.dataset.chatStyle = "terminal";
  else delete document.documentElement.dataset.chatStyle;
}

/** Persist the browser choice and apply it immediately. */
export function chooseChatStyle(choice: ChatStyle): void {
  chosen = choice;
  try { localStorage.setItem(KEY, choice); } catch { /* Private storage can be unavailable. */ }
  applyChatStyle(choice);
  listeners.forEach((listener) => listener());
}

/** The style chosen, kept current wherever it is chosen. */
export function useChatStyle(): ChatStyle {
  return useSyncExternalStore(subscribe, style);
}

/** Apply the stored style at boot; `index.html` has already done so before the first paint. */
export function installChatStyle(): void {
  applyChatStyle(readChatStyle());
}

/** The style the page shows: the one chosen here, else the one stored. */
function style(): ChatStyle {
  return chosen ?? readChatStyle();
}
