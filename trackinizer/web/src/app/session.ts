import { createContext, useContext, useEffect, useLayoutEffect, useRef } from "react";
import { APP_BASE } from "../base";
import { log } from "../debug/log";
import { currentRoute, routeUrl } from "../router/addressBar";

/** The login page, which sends the visitor back to `next`, a path of this origin, after sign-in. */
export function loginUrl(next: string): string {
  return `/auth/login_page?next=${encodeURIComponent(next)}`;
}

/** The login page, which sends the visitor back to the app's home after sign-in. */
export const LOGIN_URL = loginUrl(APP_BASE);

/**
 * The signed-in session's way out: to the login page, once, keeping work.
 *
 * `next` is the page the user was on, so sign-in returns there. Open drafts are
 * saved first by the callbacks their editors register.
 */
export class Session {
  readonly #draftSavers = new Set<() => void>();
  readonly #assign: (url: string) => void;
  #leaving = false;

  /** `assign` loads a page, as `location.assign` does. */
  constructor(assign: (url: string) => void) {
    this.#assign = assign;
  }

  /** Load `url` in place of the app, as a sign-out does. */
  assign(url: string): void {
    this.#assign(url);
  }

  /** Run `save` before leaving for the login page; the returned function stops that. */
  addDraftSaver(save: () => void): () => void {
    this.#draftSavers.add(save);
    return () => {
      this.#draftSavers.delete(save);
    };
  }

  /** Save drafts, then go to the login page, which returns to this page. Later calls do nothing. */
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
    this.#assign(loginUrl(routeUrl(currentRoute())));
  }
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
