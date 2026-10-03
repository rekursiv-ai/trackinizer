import { type CallOptions, client, send, TIMEOUT_MS } from "./client";
import type { Change } from "./detail";
import type { components } from "./generated/schema";

/** A `change_log` discriminator: a field's flat storage name, or an event such as `edge_added`. */
export type ChangeKind = components["schemas"]["trackinizer__types__change_log__Kind"];

/**
 * One `change_log` row as `GET /api/change_log` sends it.
 *
 * Both snapshots carry every column, `null` where the change did not touch it.
 * A brief row's snapshots carry only the columns that are set, and free text
 * (`title`, `description`, ...) cut to its first 32 characters; ids, statuses
 * and other values are whole. `caused_by` names the change that set this one
 * off: the second row of an edge pair names the first.
 */
export type LoggedChange = Change & {
  /** The inquiry changed; FK-free, so a purged row's changes outlive it. */
  readonly subject_id: string;
};

/** One page of one change kind or several, newest first. */
export type ChangePage = {
  /** Several kinds are read in one request: a change of any of them. */
  readonly kind: ChangeKind | readonly ChangeKind[];
  /** Only changes older than this one, in the log's `(created, id)` order: the next page. */
  readonly afterId?: string;
  /** Only changes at or after this time (ISO 8601): what arrived since the newest shown. */
  readonly since?: string;
  readonly limit: number;
  /**
   * Brief rows (see `LoggedChange`): enough to tell a set value from an unset
   * one, at a fraction of the bytes, since a description edit sends both texts.
   */
  readonly brief?: boolean;
};

/**
 * Fetch one page of `GET /api/change_log` for one change kind or several.
 *
 * The server filters before its limit, so a page of these kinds is never crowded
 * out by the `dependency_changed` alerts that fill most of the log. An `afterId`
 * the server no longer has is an `ApiError` with status 404.
 */
export async function listChanges(
  { kind, afterId, since, limit, brief }: ChangePage,
  { signal }: CallOptions = {},
): Promise<LoggedChange[]> {
  const kinds = typeof kind === "string" ? [kind] : [...kind];
  const rows = await send(TIMEOUT_MS.read, signal, (signal) =>
    client.GET("/api/change_log", {
      params: { query: { kind: kinds, after_id: afterId, since, limit, ...(brief && { brief }) } },
      signal,
    }),
  );
  // The schema marks every field optional, since each has a default; the route
  // serializes the whole `Change` dataclass, so every one is present, and a
  // brief row every top-level one.
  return rows as LoggedChange[];
}
