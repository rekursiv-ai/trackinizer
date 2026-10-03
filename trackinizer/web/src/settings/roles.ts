import type { Role } from "../api/me";

/**
 * Each role's rank, weakest first, as the server ranks them (`ROLE_ORDER` in
 * `server/auth.py`). The schema's enum names the roles but does not rank them,
 * so the ranking is copied here; the mapped type fails `tsc` when the schema
 * gains or loses a role.
 */
const RANK: { readonly [role in Role]: number } = { viewer: 0, writer: 1, admin: 2 };

/** Every role, weakest first. */
const ROLES: readonly Role[] = (Object.keys(RANK) as Role[]).sort((a, b) => RANK[a] - RANK[b]);

/**
 * The roles someone whose role is `ceiling` may hand out: theirs and weaker
 * ones. A key can never outrank the person who made it, and the server refuses
 * one that would (403). None for a role this build does not know.
 */
export function rolesUpTo(ceiling: string): Role[] {
  return Object.hasOwn(RANK, ceiling) ? ROLES.filter((role) => RANK[role] <= RANK[ceiling as Role]) : [];
}
