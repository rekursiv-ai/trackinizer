import { type CallOptions, client, send, TIMEOUT_MS } from "./client";
import type { components } from "./generated/schema";

// The token routes, like the admin ones, write no change row, so the server
// keeps no idempotency key for them and none is sent. The write layer never
// retries them on its own (`resendable` in `writes/requests.ts`).

/** A role, as the schema names them. `ROLES` ranks them. */
export type Role = components["schemas"]["RoleChangeBody"]["role"];

/**
 * The signed-in caller, as `GET /api/me/profile` returns it.
 *
 * Written by hand from `profile_route` in `server/api/auth_routes.py`: the schema
 * types the response as a free-form object.
 */
export type Profile = {
  user_id: string;
  email: string;
  name: string;
  role: string;
  last_login: string | null;
  visual_workspace_enabled: boolean;
};

/** One of the caller's API tokens, as `GET /api/me/tokens` lists it: never its secret. */
export type Token = {
  readonly id: string;
  /** The label the caller gave it. */
  readonly name: string;
  /** The start of the secret, which is all that identifies it once made. */
  readonly prefix: string;
  readonly role: Role;
  readonly created_at: string;
  readonly last_used_at: string | null;
  readonly revoked_at: string | null;
};

/** A new token, with the secret the server returns this once and never again. */
export type NewToken = Omit<Token, "created_at" | "last_used_at" | "revoked_at"> & { readonly secret: string };

/** The body of a token create: its label, and a role at most the caller's (theirs by default). */
export type CreateTokenBody = components["schemas"]["CreateTokenBody"];

/** Fetch the caller's profile. Nobody signed in is an `ApiError` with status 401. */
export async function getProfile({ signal }: CallOptions = {}): Promise<Profile> {
  const profile = await send(TIMEOUT_MS.read, signal, (signal) =>
    client.GET("/api/me/profile", { signal }),
  );
  return profile as Profile;
}

/** Persist the account's opt-in for the agent-guided canvas. */
export async function setVisualWorkspacePreference(enabled: boolean): Promise<{ enabled: boolean }> {
  return send(TIMEOUT_MS.write, undefined, (signal) =>
    client.PUT("/api/me/visual-workspace", { body: { enabled }, signal }),
  );
}

/** Fetch the caller's tokens, revoked ones included. */
export async function listTokens({ signal }: CallOptions = {}): Promise<Token[]> {
  const listed = await send(TIMEOUT_MS.read, signal, (signal) => client.GET("/api/me/tokens", { signal }));
  // Written by hand from `list_tokens_route`: the schema types it as free JSON.
  return (listed as { tokens: Token[] }).tokens;
}

/** Make a token. A role above the caller's is refused with 403. */
export async function createToken(body: CreateTokenBody, { signal }: CallOptions = {}): Promise<NewToken> {
  const made = await send(TIMEOUT_MS.write, signal, (signal) => client.POST("/api/me/tokens", { body, signal }));
  return made as NewToken;
}

/** Revoke a token at once. One not the caller's, or already revoked, is a 404. */
export async function revokeToken(id: string, { signal }: CallOptions = {}): Promise<void> {
  await send(TIMEOUT_MS.write, signal, (signal) =>
    client.POST("/api/me/tokens/{key_id}/revoke", { params: { path: { key_id: id } }, signal }),
  );
}

/** Change a token's role; one above the caller's is refused with 403. */
export async function setTokenRole(id: string, role: Role, { signal }: CallOptions = {}): Promise<void> {
  await send(TIMEOUT_MS.write, signal, (signal) =>
    client.PUT("/api/me/tokens/{key_id}/role", { params: { path: { key_id: id } }, body: { role }, signal }),
  );
}

/**
 * Sign out: the server clears the session cookie.
 *
 * It answers with a redirect to `/`, which is not followed: the redirect is the
 * answer, and a server without the old UI answers `/` with 404 (parity bug B2).
 * A request from another origin is refused with 403.
 */
export async function signOut({ signal }: CallOptions = {}): Promise<void> {
  await send(TIMEOUT_MS.write, signal, (signal) =>
    client.POST("/auth/logout", { parseAs: "stream", redirect: "manual", signal }),
  );
}
