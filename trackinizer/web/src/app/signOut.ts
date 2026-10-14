import { useContext, useState } from "react";
import { signOut } from "../api/me";
import { LOGIN_URL, SessionContext } from "./session";

/**
 * The account a server run with `--no-auth` answers every request as
 * (`server/auth.py`). Nobody signs in there, so signing out would only land on
 * a login page with nothing to sign in with.
 */
export const NO_AUTH_EMAIL = "no-auth@localhost";

/** Why signing out failed: the message to show, and the error for Copy details. */
export type SignOutFailure = { readonly message: string; readonly error: unknown };

/**
 * Sign out, from Settings or the sidebar: the server clears the session cookie,
 * then the login page loads, by `assign` or else the app's own (`Session`).
 * `failed` hears of a failure, which the state holds as well.
 *
 * Not a write through the write layer: it changes no data, a second request
 * does no harm, and its failures are about signing out, not saving.
 */
export function useSignOut({ assign, failed }: {
  readonly assign?: (url: string) => void;
  readonly failed?: (failure: SignOutFailure) => void;
} = {}): { readonly pending: boolean; readonly failure: SignOutFailure | null; readonly leave: () => Promise<void> } {
  const session = useContext(SessionContext);
  const [state, setState] = useState<{ readonly pending: boolean; readonly failure: SignOutFailure | null }>(IDLE);
  const leave = async () => {
    if (state.pending) return;
    setState({ pending: true, failure: null });
    try {
      await signOut();
    } catch (error) {
      const failure = { message: error instanceof Error ? error.message : String(error), error };
      setState({ pending: false, failure });
      failed?.(failure);
      return;
    }
    if (assign) assign(LOGIN_URL);
    else if (session) session.assign(LOGIN_URL);
    else throw new Error("useSignOut needs `assign` or a SessionContext above it.");
  };
  return { ...state, leave };
}

const IDLE = { pending: false, failure: null };
