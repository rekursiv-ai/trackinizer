import { createContext, useContext, useEffect, useLayoutEffect, useRef } from "react";
import { log } from "../debug/log";

/** The login page, which sends the visitor back to the app after sign-in. */
export const LOGIN_URL = `/auth/login_page?next=${encodeURIComponent("/app/")}`;

/**
 * The signed-in session's way out: to the login page, once, keeping work.
 *
 * `next` returns to `/app/` only, as in the old UI, so the hash the user was on
 * waits in this tab's `sessionStorage`, and the next boot restores it
 * (`restoreReturnHash`). Open drafts are saved first by the callbacks their
 * editors register.
 */
export class Session {
  readonly #draftSavers = new Set<() => void>();
  readonly #assign: (url: string) => void;
  #leaving = false;

  /** `assign` loads a page, as `location.assign` does. */
  constructor(assign: (url: string) => void) {
    this.#assign = assign;
  }

  /** Run `save` before leaving for the login page; the returned function stops that. */
  addDraftSaver(save: () => void): () => void {
    this.#draftSavers.add(save);
    return () => {
      this.#draftSavers.delete(save);
    };
  }

  /** Save drafts and the current hash, then go to the login page. Later calls do nothing. */
  leaveForLogin(): void {
    if (this.#leaving) return;
    this.#leaving = true;
    log("warn", "session.ended", { drafts: this.#draftSavers.size });
    for (const save of this.#draftSavers) {
      // One editor's failure must not keep a signed-out user from signing in.
      try {
        save();
      } catch (error) {
        console.error("Could not save a draft before signing in again.", error);
      }
    }
    try {
      if (location.hash) sessionStorage.setItem(RETURN_HASH_KEY, location.hash);
    } catch {
      // Storage off or full: sign-in still works; the user lands on the default view.
    }
    this.#assign(LOGIN_URL);
  }
}

/**
 * Put back the hash saved before a sign-in, unless the page opened on a hash of
 * its own. Call once, before the router reads the address bar.
 */
export function restoreReturnHash(): void {
  let saved: string | null;
  try {
    saved = sessionStorage.getItem(RETURN_HASH_KEY);
    sessionStorage.removeItem(RETURN_HASH_KEY);
  } catch {
    return;
  }
  if (saved && !location.hash) history.replaceState(history.state, "", saved);
}

export const SessionContext = createContext<Session | null>(null);

/**
 * Save an open draft with `save` if the session ends while it is mounted.
 *
 * A 401 mid-session leaves for the login page; `save` runs just before, and
 * should write the draft to `localStorage` for its editor to restore.
 */
export function useDraftSaver(save: () => void): void {
  const session = useContext(SessionContext);
  if (!session) throw new Error("useDraftSaver needs a SessionContext above it.");
  const latest = useRef(save);
  useLayoutEffect(() => {
    latest.current = save;
  });
  useEffect(() => session.addDraftSaver(() => latest.current()), [session]);
}

const RETURN_HASH_KEY = "trackinizer.v2.return_hash";
