import { useSyncExternalStore } from "react";
import { setPreferences } from "./api/me";
import type { components } from "./api/generated/schema";
import { log } from "./debug/log";

/**
 * The user's preferences: theme, Chat's style and side, and the like. They live on
 * the server (`server/preferences.py`), on the user's row, so any browser or
 * machine finds them and an agent the user works with can change them; this page
 * keeps none of them. The server always answers every key, its default filled in.
 */
export type Preferences = Required<components["schemas"]["Preferences"]>;

/** What the page shows until the profile brings the user's own: the server's defaults. */
const DEFAULTS: Preferences = { theme: "dark", chat_style: "default", chat_side: "left", highlight_mentions: true };

/** The keys this browser kept before they moved to the server, read once and then removed. */
const LEGACY = {
  theme: "trackinizer.theme",
  chat_style: "trackinizer.chat.style",
  chat_side: "trackinizer.chat.side",
  highlight_mentions: (email: string) => `trackinizer.v2.highlight-mentions.${email}`,
} as const;

let current: Preferences = DEFAULTS;

/** Changes on their way to the server: a profile read meanwhile would undo what they show. */
let sending = 0;

const listeners = new Set<() => void>();

/** The preferences the page shows now. */
export function preferences(): Preferences {
  return current;
}

/** Be told of every change, chosen here or read from the server. */
export function subscribePreferences(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The preferences, kept current. */
export function usePreferences(): Preferences {
  return useSyncExternalStore(subscribePreferences, preferences);
}

function show(next: Preferences): void {
  if ((Object.keys(next) as (keyof Preferences)[]).every((key) => next[key] === current[key])) return;
  current = next;
  for (const listener of [...listeners]) listener();
}

/**
 * Take the server's word for the user's preferences (the profile), unless a change
 * is on its way. A server older than preferences sends none: the defaults stay.
 */
export function receivePreferences(next: Preferences | undefined): void {
  if (next && sending === 0) show(next);
}

/**
 * Change preferences: shown at once, then sent. A change the server did not take
 * (offline, say) stays on this page and is not kept: the next profile read brings
 * the server's back. Resolves whether the server took it.
 */
export async function choosePreferences(change: Partial<Preferences>): Promise<boolean> {
  show({ ...current, ...change });
  sending += 1;
  try {
    const saved = await setPreferences(change);
    sending -= 1;
    if (sending === 0) show(saved);
    return true;
  } catch (error) {
    sending -= 1;
    log("warn", "preferences.not_kept", { keys: Object.keys(change).join(","), error: String(error) });
    return false;
  }
}

/**
 * Send what this browser kept before preferences moved to the server, once: the
 * server takes it, then this browser forgets it. Nothing to send, nothing is sent.
 */
export async function adoptLegacyPreferences(email: string): Promise<void> {
  const keys = [LEGACY.theme, LEGACY.chat_style, LEGACY.chat_side, LEGACY.highlight_mentions(email)];
  let kept: (string | null)[];
  try {
    kept = keys.map((key) => localStorage.getItem(key));
  } catch {
    return;
  }
  const [theme, style, side, mentions] = kept;
  const change: Partial<Preferences> = {
    ...(theme === "dark" || theme === "light" || theme === "system" ? { theme } : {}),
    ...(style === "default" || style === "terminal" ? { chat_style: style } : {}),
    ...(side === "left" || side === "right" ? { chat_side: side } : {}),
    ...(mentions === "off" ? { highlight_mentions: false } : {}),
  };
  if (Object.keys(change).length > 0 && !await choosePreferences(change)) return;
  try {
    for (const key of keys) localStorage.removeItem(key);
  } catch { /* Storage can be unavailable; then there is nothing to forget either. */ }
}
