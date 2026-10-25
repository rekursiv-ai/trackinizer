import { useSyncExternalStore } from "react";
import { setPreferences } from "./api/me";
import type { components } from "./api/generated/schema";
import { log } from "./debug/log";
import { sameJson } from "./state/value";

/**
 * The user's preferences: theme, skin and the like, and the
 * user's own data: stars, saved views, names, read marks and unsent drafts. They
 * live on the server (`server/preferences.py`), on the user's row, so any browser
 * or machine finds them and an agent the user works with can change them; this
 * page keeps none of them. The server always answers every key, its default filled
 * in. A change replaces a top-level key whole.
 */
export type Preferences = Required<components["schemas"]["Preferences"]>;

/** What the page shows until the profile brings the user's own: the server's defaults. */
export const DEFAULT_PREFERENCES: Preferences = {
  theme: "dark",
  skin: "default",
  chat_side: "left",
  app_bar: "top",
  app_bar_width: 220,
  app_bar_folded: false,
  highlight_mentions: true,
  terminal_font_size: "13px",
  terminal_tab_chords: false,
  terminal_placement: "bottom",
  home: "graph",
  chat_provider: "assistant",
  chat_tool_calls: false,
  stars: [],
  views: [],
  console_views: [],
  aliases: [],
  people: {},
  read: { boundary: null, marks: [] },
  collapsed: [],
  dismissed: [],
  lens: null,
  drafts: {},
  create_draft: null,
  workspaces: { order: [], pinned: [], hidden: [] },
  settings_artifact_id: null,
};

/** The keys this browser kept before they moved to the server, read once and then removed. */
const LEGACY = {
  theme: "trackinizer.theme",
  // Chat has no style now; its old key is still forgotten, unread.
  style: "trackinizer.chat.style",
  chat_side: "trackinizer.chat.side",
  highlight_mentions: (email: string) => `trackinizer.v2.highlight-mentions.${email}`,
} as const;

let current: Preferences = DEFAULT_PREFERENCES;

/** Changes on their way to the server: a profile read meanwhile would undo what they show. */
let sending = 0;

/** The last change sent, so the next one goes after it: two on the wire at once could land out of order. */
let last: Promise<unknown> = Promise.resolve();

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

/**
 * Show `next`. A key whose value is the one shown keeps its object, so what reads
 * only that key does not render again; nothing changed, nobody is told.
 */
function show(next: Preferences): void {
  const kept = Object.fromEntries(
    (Object.keys(next) as (keyof Preferences)[]).map((key) => [key, sameJson(next[key], current[key]) ? current[key] : next[key]]),
  ) as Preferences;
  if ((Object.keys(kept) as (keyof Preferences)[]).every((key) => kept[key] === current[key])) return;
  current = kept;
  for (const listener of [...listeners]) listener();
}

/**
 * Take the server's word for the user's preferences (the profile), unless a change
 * is on its way. A server older than preferences sends none, and one older than a
 * key sends it not: the defaults stay.
 */
export function receivePreferences(next: Partial<Preferences> | undefined): void {
  if (next && sending === 0) show(known({ ...DEFAULT_PREFERENCES, ...next }));
}

/** `values` without a key this page does not know: one an older server still sends (`chat_style`) is dropped, not shown. */
function known(values: Preferences): Preferences {
  return Object.fromEntries(Object.entries(values).filter(([key]) => key in DEFAULT_PREFERENCES)) as Preferences;
}

/**
 * Change preferences: shown at once, then sent. A change the server did not take
 * (offline, say) stays on this page and is not kept: the next profile read brings
 * the server's back. Resolves whether the server took it. `keepalive` lets the
 * request outlive the page, for a change made as it closes.
 */
export async function choosePreferences(change: Partial<Preferences>, { keepalive = false } = {}): Promise<boolean> {
  show({ ...current, ...change });
  sending += 1;
  const turn = last.then(() => setPreferences(change, { keepalive }));
  last = turn.catch(() => undefined);
  const keys = Object.keys(change) as (keyof Preferences)[];
  try {
    const saved = await turn;
    sending -= 1;
    if (sending === 0) show(known({ ...DEFAULT_PREFERENCES, ...saved }));
    return true;
  } catch (error) {
    sending -= 1;
    log("warn", "preferences.not_kept", { keys: keys.join(","), error: String(error) });
    return false;
  }
}

/** How long typing pauses before a change made with `choosePreferencesSoon` is sent. */
export const PAUSE_MS = 500;

/** The keys changed with `choosePreferencesSoon` and not yet sent. */
const waiting = new Set<keyof Preferences>();

let pause: ReturnType<typeof setTimeout> | undefined;

/** Whether the page-close listener is added. */
let listening = false;

/**
 * Change preferences that change as the user types, a draft's: shown at once, and
 * sent after typing pauses, one request however many keys were typed. The page
 * closing sends what waits. A change that leaves them as they are does nothing.
 * Meanwhile a profile read does not undo it, as for `choosePreferences`.
 */
export function choosePreferencesSoon(change: Partial<Preferences>): void {
  const keys = (Object.keys(change) as (keyof Preferences)[]).filter((key) => !sameJson(change[key], current[key]));
  if (keys.length === 0) return;
  show({ ...current, ...change });
  if (waiting.size === 0) sending += 1;
  if (!listening) {
    listening = true;
    addEventListener("pagehide", () => void sendWaiting({ keepalive: true }));
  }
  for (const key of keys) waiting.add(key);
  clearTimeout(pause);
  pause = setTimeout(() => void sendWaiting({ keepalive: false }), PAUSE_MS);
}

/**
 * Send what `choosePreferencesSoon` holds now. The page closing has no time to wait
 * for the answer, so `keepalive` lets the request finish; the browser caps such a
 * body at 64 KB, so only that send asks for it. Resolves once the server has
 * answered it and every change chosen before.
 */
export async function sendWaiting({ keepalive = false } = {}): Promise<void> {
  clearTimeout(pause);
  if (waiting.size > 0) {
    const change = Object.fromEntries([...waiting].map((key) => [key, current[key]]));
    waiting.clear();
    const sent = choosePreferences(change, { keepalive });
    // It counts itself now; the hold `choosePreferencesSoon` put up is over.
    sending -= 1;
    await sent;
  }
  await last;
}

/**
 * Send what this browser kept before preferences moved to the server, once: the
 * server takes it, then this browser forgets it. Nothing to send, nothing is sent.
 */
export async function adoptLegacyPreferences(email: string): Promise<void> {
  const keys = [LEGACY.theme, LEGACY.style, LEGACY.chat_side, LEGACY.highlight_mentions(email)];
  let kept: (string | null)[];
  try {
    kept = keys.map((key) => localStorage.getItem(key));
  } catch {
    return;
  }
  const [theme, , side, mentions] = kept;
  const change: Partial<Preferences> = {
    ...(theme === "dark" || theme === "light" || theme === "system" ? { theme } : {}),
    ...(side === "left" || side === "right" ? { chat_side: side } : {}),
    ...(mentions === "off" ? { highlight_mentions: false } : {}),
  };
  if (Object.keys(change).length > 0 && !await choosePreferences(change)) return;
  try {
    for (const key of keys) localStorage.removeItem(key);
  } catch { /* Storage can be unavailable; then there is nothing to forget either. */ }
}
