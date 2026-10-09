import { useSyncExternalStore } from "react";
import { useProfile } from "../app/boot";

/**
 * Whether the rows the assistant's answers mention light up on the canvas. On until
 * the user turns it off in Settings; kept per user in this browser, as the
 * theme is, and read at the moment an answer arrives.
 */
const key = (email: string) => `trackinizer.v2.highlight-mentions.${email}`;

/** Told of every choice made on this page, and of another tab's. */
const listeners = new Set<() => void>();

/**
 * What was chosen on this page, which holds whether or not storage kept it:
 * read back from a storage that refused it, the switch would stay on.
 */
const chosen = new Map<string, boolean>();

/** Whether `email`'s answers' mentions are highlighted: the choice made here, else the stored one, else on. */
export function readHighlightMentions(email: string): boolean {
  const here = chosen.get(email);
  if (here !== undefined) return here;
  try {
    return localStorage.getItem(key(email)) !== "off";
  } catch {
    return true;
  }
}

/** Turn highlighting of mentions on or off for `email`; every hook showing it updates. */
export function chooseHighlightMentions(email: string, on: boolean): void {
  chosen.set(email, on);
  try {
    if (on) localStorage.removeItem(key(email));
    else localStorage.setItem(key(email), "off");
  } catch { /* Private storage can be unavailable. */ }
  for (const listener of [...listeners]) listener();
}

/** The signed-in user's switch and the way to set it. */
export function useHighlightMentions(): readonly [boolean, (on: boolean) => void] {
  const { email } = useProfile();
  const on = useSyncExternalStore(subscribe, () => readHighlightMentions(email));
  return [on, (next) => chooseHighlightMentions(email, next)] as const;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  // Another tab's choice reaches this one as a storage event; it replaces what this page chose.
  const other = () => {
    chosen.clear();
    listener();
  };
  addEventListener("storage", other);
  return () => {
    listeners.delete(listener);
    removeEventListener("storage", other);
  };
}
