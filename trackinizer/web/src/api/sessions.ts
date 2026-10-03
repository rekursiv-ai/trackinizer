import { type CallOptions, client, send, TIMEOUT_MS } from "./client";
import type { components } from "./generated/schema";

/**
 * One file an AgentSession was captured from, as the parts listing names it.
 * `records` is how many records it holds. Part `-1` holds turns backfilled
 * from the retired event log, with no `format`.
 */
export type SessionPart = components["schemas"]["PartBody"];

/**
 * One stored record of a transcript. `kind` is the record's class
 * (`UserMessage`, `ToolCall`); `payload` is the record as the server's
 * dataclass codec wrote it; `text` is its search projection.
 */
export type SessionRecord = components["schemas"]["RecordBody-Output"];
export type RecentSessionTurn = components["schemas"]["RecentTurnBody"];

/** Read the newest conversational turns without transferring intervening tools. */
export async function readRecentSessionTurns(
  sessionId: string,
  { signal }: CallOptions = {},
): Promise<RecentSessionTurn[]> {
  const body = await send(TIMEOUT_MS.read, signal, (signal) =>
    client.GET("/api/sessions/{session_id}/turns", {
      params: { path: { session_id: sessionId }, query: { limit: 20 } }, signal,
    }));
  return body.turns ?? [];
}

/** Where a page of a part starts, and how long it is. */
export type RecordsPage = {
  readonly part: number;
  /** Only records after this `idx`; `-1` for the first page. */
  readonly afterIdx: number;
  /** At most 1,000, the server's cap. */
  readonly limit: number;
};

/** Fetch `GET /api/sessions/{id}/parts`: every part of the session, in `part` order. */
export async function listSessionParts(sessionId: string, { signal }: CallOptions = {}): Promise<SessionPart[]> {
  const body = await send(TIMEOUT_MS.read, signal, (signal) =>
    client.GET("/api/sessions/{session_id}/parts", { params: { path: { session_id: sessionId } }, signal }),
  );
  return body.parts ?? [];
}

/**
 * Fetch one page of a part's records, in `idx` order.
 *
 * `afterIdx` is an exclusive bound, not an offset, so pages stay stable while
 * a capture is still appending. A page shorter than `limit` is the part's end.
 * It asks for `plaintext_only`: sealed reasoning is base64 with nothing to read,
 * and the largest thing on a row; only a replay needs it.
 */
export async function readSessionRecords(
  sessionId: string,
  { part, afterIdx, limit }: RecordsPage,
  { signal }: CallOptions = {},
): Promise<SessionRecord[]> {
  const body = await send(TIMEOUT_MS.read, signal, (signal) =>
    client.GET("/api/sessions/{session_id}/records", {
      params: {
        path: { session_id: sessionId },
        query: { part, after_idx: afterIdx, limit, plaintext_only: true },
      },
      signal,
    }),
  );
  return body.records ?? [];
}

/** What the server answers a message it queued: how many wait for the session's poller. */
export type InboundReceipt = components["schemas"]["InboundEnqueueResponse"];

/**
 * Send `POST /api/sessions/{id}/inbound`: queue `text` for the session's
 * `trax run` to type into its terminal, under idempotency `key`, which a retry
 * reuses so the message lands once. The server records the signed-in user as
 * the sender, and answers 409 when the session has ended or no `trax run` is
 * polling it.
 */
export async function sendSessionMessage(sessionId: string, text: string, key: string): Promise<InboundReceipt> {
  return send(TIMEOUT_MS.write, undefined, (signal) =>
    client.POST("/api/sessions/{session_id}/inbound", {
      params: { path: { session_id: sessionId } },
      body: { text },
      headers: { "Idempotency-Key": key },
      signal,
    }),
  );
}

/** One captured record in the cross-session feed, with its session's routing name and rooms. */
export type FeedEvent = components["schemas"]["FeedEvent"];

/** Where a feed page ends: the next page resumes strictly past it. */
export type FeedCursor = components["schemas"]["FeedCursor"];

/** One page of the feed, oldest first, and the cursor to resume past. */
export type FeedPage = components["schemas"]["FeedResponse"];

/**
 * Which records a feed read keeps, by session routing name, room, CLI and record
 * kind. A record passes a filter when it matches any of its values, and must
 * pass every filter given; an empty or absent one keeps everything.
 */
export type FeedFilters = {
  readonly actor?: readonly string[];
  readonly room?: readonly string[];
  readonly cli?: readonly string[];
  readonly kind?: readonly string[];
};

/** Which page of the feed to read: its newest (`tail`), past a cursor, or within a window. */
export type FeedRead = FeedFilters & {
  readonly tail?: boolean;
  /** Only what a person or agent said: the records the facets count as `conversation`. */
  readonly conversation?: boolean;
  readonly after?: FeedCursor;
  /** ISO times; `since` and `until` are both inclusive. */
  readonly since?: string;
  readonly until?: string;
  /** At most 1,000, the server's cap. */
  readonly limit: number;
};

/**
 * Fetch `GET /api/web/feed`: every session's captured records interleaved by
 * when the server wrote them. The cursor is all four of its fields, since a
 * page can end inside a group written in the same instant.
 */
export async function readFeed(
  { tail, conversation, after, since, until, limit, ...filters }: FeedRead,
  { signal }: CallOptions = {},
): Promise<FeedPage> {
  return send(TIMEOUT_MS.read, signal, (signal) =>
    client.GET("/api/web/feed", {
      params: {
        query: {
          ...(tail ? { tail } : {}),
          ...(conversation ? { conversation } : {}),
          ...(after
            ? { after_created: after.created, after_session: after.session_id, after_part: after.part, after_seq: after.seq }
            : {}),
          ...(since ? { since } : {}),
          ...(until ? { until } : {}),
          limit,
          ...filterQuery(filters),
        },
      },
      signal,
    }),
  );
}

/** One session's share of a feed window: its records, how many are conversation, its newest, and when it ended. */
export type FeedActorFacet = components["schemas"]["FeedActorFacet"];

/** What a window of the feed holds, by session (newest first), room and record kind (largest first). */
export type FeedFacets = components["schemas"]["FeedFacetsResponse"];

/** Which window of the feed to count, under the feed's filters; ISO times, both inclusive, either open. */
export type FacetsRead = FeedFilters & {
  readonly since?: string;
  readonly until?: string;
};

/**
 * Fetch `GET /api/web/feed/facets`: the window's records counted by session,
 * room and record kind, with each session's conversation among them. The
 * server reads every record in the window, so a bounded one answers fastest.
 */
export async function readFeedFacets({ since, until, ...filters }: FacetsRead, { signal }: CallOptions = {}): Promise<FeedFacets> {
  return send(TIMEOUT_MS.read, signal, (signal) =>
    client.GET("/api/web/feed/facets", {
      params: { query: { ...(since ? { since } : {}), ...(until ? { until } : {}), ...filterQuery(filters) } },
      signal,
    }),
  );
}

/** The filters as query parameters, each value repeated and empty ones left out. */
function filterQuery({ actor, room, cli, kind }: FeedFilters) {
  return {
    ...(actor?.length ? { actor: [...actor] } : {}),
    ...(room?.length ? { room: [...room] } : {}),
    ...(cli?.length ? { cli: [...cli] } : {}),
    ...(kind?.length ? { kind: [...kind] } : {}),
  };
}

/** Where a routed message goes: an agent's routing name, in one of its rooms or `null` for its only one. */
export type MessageTarget = { readonly actor: string; readonly room: string | null };

/**
 * Send `POST /api/messages`: queue `text` for every live session `target` names,
 * under idempotency `key`, which a retry reuses. The answer lists the sessions
 * it was queued for; none means no live session matched. A bare agent in more
 * than one room is a 409.
 */
export async function sendRoutedMessage(
  target: MessageTarget,
  text: string,
  key: string,
): Promise<components["schemas"]["SendMessageResponse"]> {
  return send(TIMEOUT_MS.write, undefined, (signal) =>
    client.POST("/api/messages", {
      body: { actor: target.actor, room: target.room, text },
      headers: { "Idempotency-Key": key },
      signal,
    }),
  );
}
