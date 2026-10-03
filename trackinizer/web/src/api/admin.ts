import { type CallOptions, client, send, TIMEOUT_MS } from "./client";
import type { components } from "./generated/schema";
import type { Role } from "./me";

// Admin routes answer 403 to anyone but an admin. They write no change row, so
// the server keeps no idempotency key for them and none is sent; the write
// layer never retries them on its own (`resendable` in `writes/requests.ts`).
// Refusals that protect the org come back as 409: an admin demoting, disabling
// or deleting themselves, a change that would leave no active admin, and a
// duplicate allowlist entry.

/** A user, as `GET /api/admin/users` lists them (written by hand from `admin_list_users_route`). */
export type User = {
  readonly id: string;
  readonly email: string;
  readonly name: string;
  readonly role: Role;
  /** `active` or `disabled`. */
  readonly status: string;
  readonly created_at: string;
  readonly last_login: string | null;
};

/** Who may sign in, and as what (written by hand from `_serialize_allowlist`). */
export type AllowlistEntry = {
  /** An address, `ada@example.com`, or a domain wildcard, `*@example.com`; stored lowercase. */
  readonly email_or_pattern: string;
  /** The role a user gets on their first sign-in through this entry; later sign-ins keep theirs. */
  readonly role: Role;
  /** The id of the admin who added it; null for the bootstrap entry. */
  readonly added_by: string | null;
  readonly added_at: string;
};

/** The body of an allowlist add. */
export type AllowlistAddBody = components["schemas"]["AllowlistAddBody"];

/** Fetch every user, newest first. */
export async function listUsers({ signal }: CallOptions = {}): Promise<User[]> {
  const listed = await send(TIMEOUT_MS.read, signal, (signal) => client.GET("/api/admin/users", { signal }));
  return (listed as { users: User[] }).users;
}

/** Change a user's role. */
export async function setUserRole(id: string, role: Role, { signal }: CallOptions = {}): Promise<void> {
  await send(TIMEOUT_MS.write, signal, (signal) =>
    client.PUT("/api/admin/users/{user_id}/role", { params: { path: { user_id: id } }, body: { role }, signal }),
  );
}

/** Disable a user; the server revokes every token they hold, and enabling does not restore them. */
export async function disableUser(id: string, { signal }: CallOptions = {}): Promise<void> {
  await send(TIMEOUT_MS.write, signal, (signal) =>
    client.POST("/api/admin/users/{user_id}/disable", { params: { path: { user_id: id } }, signal }),
  );
}

/** Enable a disabled user. */
export async function enableUser(id: string, { signal }: CallOptions = {}): Promise<void> {
  await send(TIMEOUT_MS.write, signal, (signal) =>
    client.POST("/api/admin/users/{user_id}/enable", { params: { path: { user_id: id } }, signal }),
  );
}

/** Delete a user and their tokens; changes they made keep their actor. The route takes no body. */
export async function deleteUser(id: string, { signal }: CallOptions = {}): Promise<void> {
  await send(TIMEOUT_MS.write, signal, (signal) =>
    client.DELETE("/api/admin/users/{user_id}", { params: { path: { user_id: id } }, signal }),
  );
}

/** Fetch the allowlist, newest first. */
export async function listAllowlist({ signal }: CallOptions = {}): Promise<AllowlistEntry[]> {
  const listed = await send(TIMEOUT_MS.read, signal, (signal) => client.GET("/api/admin/allowlist", { signal }));
  return (listed as { entries: AllowlistEntry[] }).entries;
}

/** Add an address or a domain wildcard; resolves with the entry as stored, trimmed and lowercase. */
export async function addAllowlistEntry(
  body: AllowlistAddBody,
  { signal }: CallOptions = {},
): Promise<Pick<AllowlistEntry, "email_or_pattern" | "role">> {
  const added = await send(TIMEOUT_MS.write, signal, (signal) => client.POST("/api/admin/allowlist", { body, signal }));
  // Written by hand from `admin_add_allowlist_route`: the schema types it as free JSON.
  const { email_or_pattern, role } = added as AllowlistEntry;
  return { email_or_pattern, role };
}

/** Change the role an entry grants. The entry goes in the path, percent-encoded. */
export async function setAllowlistRole(entry: string, role: Role, { signal }: CallOptions = {}): Promise<void> {
  await send(TIMEOUT_MS.write, signal, (signal) =>
    client.PUT("/api/admin/allowlist/{email_or_pattern}/role", {
      params: { path: { email_or_pattern: entry } },
      body: { role },
      signal,
    }),
  );
}

/**
 * Remove an entry. Users it let in keep their accounts, sessions and tokens, but
 * the next sign-in of one it alone matched is refused.
 */
export async function removeAllowlistEntry(entry: string, { signal }: CallOptions = {}): Promise<void> {
  await send(TIMEOUT_MS.write, signal, (signal) =>
    client.DELETE("/api/admin/allowlist/{email_or_pattern}", { params: { path: { email_or_pattern: entry } }, signal }),
  );
}
