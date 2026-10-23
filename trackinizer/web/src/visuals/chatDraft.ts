import { useCallback, useContext, useLayoutEffect, useState, useSyncExternalStore } from "react";
import { useProfile } from "../app/boot";
import { type Session, SessionContext, useDraftSaver } from "../app/session";
import { choosePreferencesSoon, preferences, subscribePreferences, unsavedKeys } from "../preferences";
import { forgetFieldDraftHere, keepFieldDraftHere } from "../state/local";

/** The most drafts the server keeps (`Preferences.drafts` in `server/preferences.py`). */
const MAX_DRAFTS = 200;

/** A canvas's Chat prompt: its text, how to change it, and why it is not kept, if it is not. */
export type ChatDraft = {
  readonly text: string;
  readonly edit: (text: string) => void;
  /**
   * Clear the draft as it stands now. Take it when a send starts and call it once
   * the send succeeds: it clears this canvas's draft, for this user, only if
   * nothing was typed there since; another canvas's, another user's, or a later
   * draft, typed here or elsewhere, stays.
   */
  readonly clear: () => void;
  readonly problem: string | null;
};

/**
 * The unsent prompt of the Chat of canvas `canvasId`, kept in the user's
 * preferences under `chat:<canvasId>` so it reopens in any browser.
 *
 * What was typed in this session wins over the preferences the server answers, so
 * a send that failed, or a store already at its 200 drafts, loses nothing shown;
 * `problem` says when the text is not kept. With no canvas the text is the
 * component's alone, nothing is kept, and it does not follow into a canvas.
 */
export function useChatDraft(canvasId: string | null): ChatDraft {
  const { email } = useProfile();
  const session = useContext(SessionContext);
  if (!session) throw new Error("useChatDraft needs a SessionContext above it.");
  const store = storeOf(session);
  useLayoutEffect(() => {
    store.email = email;
  }, [store, email]);
  const key = canvasId === null ? null : `chat:${canvasId}`;
  const slot = `${email}\u0000${key}`;
  const local = useSyncExternalStore(store.subscribe, () => store.typed.get(slot));
  const drafts = useSyncExternalStore(subscribePreferences, () => preferences().drafts);
  const failed = useSyncExternalStore(subscribePreferences, () => unsavedKeys().has("drafts"));
  const [loose, setLoose] = useState({ id: canvasId, text: "", serial: 0 });
  if (loose.id !== canvasId) setLoose({ id: canvasId, text: "", serial: 0 });
  const text = key === null ? loose.text : (local?.text ?? drafts[key]?.text ?? "");
  useDraftSaver(() => {
    for (const [typedSlot, { text: typedText, key: typedKey }] of store.typed) {
      if (typedSlot.startsWith(`${email}\u0000`) && typedText) keepFieldDraftHere(email, typedKey, { text: typedText, base: null });
    }
  });
  const edit = useCallback(
    (next: string) =>
      key === null ? setLoose((now) => ({ id: canvasId, text: next, serial: now.serial + 1 })) : write(store, email, key, next),
    [store, email, key, canvasId],
  );
  const serial = key === null ? loose.serial : (local?.serial ?? 0);
  const clear = useCallback(() => {
    if (key === null) setLoose((now) => (now.id === canvasId && now.serial === serial ? { ...now, text: "", serial: now.serial + 1 } : now));
    else forget(store, email, key, serial, text);
  }, [store, email, key, serial, text, canvasId]);
  return { text, edit, clear, problem: key === null || text === "" ? null : problemOf(drafts, key, local?.text != null && (failed || drafts[key]?.text !== local.text)) };
}

function problemOf(drafts: { readonly [key: string]: unknown }, key: string, failed: boolean): string | null {
  if (!(key in drafts) && Object.keys(drafts).length >= MAX_DRAFTS) {
    return `Draft not saved: kept in this page only, as you already have ${MAX_DRAFTS} saved drafts, the most the server keeps.`;
  }
  return failed ? "Draft not saved: the server did not take it; it is kept in this page and sent with your next edit." : null;
}

/**
 * What one signed-in session typed, per user and canvas: the text, or null to
 * show the preferences' again, and the slot's edit count a stale `clear` checks.
 */
type Store = {
  /** The user the page shows now: a `clear` or `edit` held from another never changes their preferences. */
  email: string;
  readonly typed: Map<string, { readonly key: string; readonly text: string | null; readonly serial: number }>;
  readonly subscribe: (listener: () => void) => () => void;
  readonly notify: () => void;
};

const stores = new WeakMap<Session, Store>();

function storeOf(session: Session): Store {
  const known = stores.get(session);
  if (known) return known;
  const listeners = new Set<() => void>();
  const store: Store = {
    email: "",
    typed: new Map(),
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    notify: () => {
      for (const listener of [...listeners]) listener();
    },
  };
  stores.set(session, store);
  return store;
}

function write(store: Store, email: string, key: string, text: string): void {
  setTyped(store, email, key, text);
  if (email !== store.email) return;
  forgetFieldDraftHere(email, key);
  // Every text this user typed, emptied ones included, rides along: a failed save or a later server
  // snapshot may have dropped one, and this save must not acknowledge it unsent. Deletions go first
  // and a key already kept keeps its place, so a new key takes only room that is free.
  const mine = [...store.typed].filter(([slot, typed]) => slot.startsWith(`${email}\u0000`) && typed.text !== null);
  let drafts = preferences().drafts;
  for (const [, typed] of mine) if (typed.text === "") drafts = without(drafts, typed.key);
  const next = { ...drafts };
  for (const [, typed] of mine) {
    if (!typed.text || (!(typed.key in next) && Object.keys(next).length >= MAX_DRAFTS)) continue;
    next[typed.key] = { text: typed.text, base: null };
  }
  choosePreferencesSoon({ drafts: next });
}

/** Clear `key`'s draft if its slot is still at `serial`; a kept draft only while it still holds `text`. */
function forget(store: Store, email: string, key: string, serial: number, text: string): void {
  if ((store.typed.get(`${email}\u0000${key}`)?.serial ?? 0) !== serial) return;
  setTyped(store, email, key, null);
  const drafts = preferences().drafts;
  if (email === store.email && drafts[key]?.text === text) choosePreferencesSoon({ drafts: without(drafts, key) });
}

function setTyped(store: Store, email: string, key: string, text: string | null): void {
  const slot = `${email}\u0000${key}`;
  store.typed.set(slot, { key, text, serial: (store.typed.get(slot)?.serial ?? 0) + 1 });
  store.notify();
}

function without<T>(drafts: { readonly [key: string]: T }, key: string): { [key: string]: T } {
  return Object.fromEntries(Object.entries(drafts).filter(([other]) => other !== key));
}
