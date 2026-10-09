import { type QueryClient, useQueryClient } from "@tanstack/react-query";
import { useContext, useEffect } from "react";
import { listSessionParts, readSessionRecords } from "../api/sessions";
import { LiveContext } from "../live";
import { refetchFresh } from "../live/cache";
import type { ChatPart } from "./chatLines";

/** Records one request reads; the server's cap. */
const PAGE = 1_000;

/**
 * How long Chat looks for a conversation's session after a line, and how often: the
 * session appears when the assistant opens it, after it hears the line. A test
 * shortens both.
 */
export const OPENING = { everyMs: 1_500, giveUpMs: 30_000 };

/** The cache key of a science chat session's records, held part by part. */
export const chatRecordsKey = (sessionId: string | null) => ["chat", "records", sessionId] as const;

/**
 * Read a science chat session's records: each part the session lists, in part
 * order, reading only the records after those `held` already holds. A run of
 * the assistant makes a part of its own, so a long chat has a few.
 */
export async function readChatParts(
  sessionId: string,
  held: readonly ChatPart[] | undefined,
  signal?: AbortSignal,
): Promise<ChatPart[]> {
  const listed = (await listSessionParts(sessionId, { signal })).filter(({ part }) => part >= 0);
  const parts: ChatPart[] = [];
  for (const { part, records } of listed) {
    const before = held?.find((each) => each.part === part)?.records ?? [];
    const read = [...before];
    // The listing counts records the server may not have written yet, so a short page ends the read.
    while (records > read.length) {
      const page = await readSessionRecords(sessionId, { part, afterIdx: read.at(-1)?.idx ?? -1, limit: PAGE }, { signal });
      read.push(...page);
      if (page.length < PAGE) break;
    }
    parts.push({ part, records: read });
  }
  return parts;
}

/**
 * Keep a science chat's records current while its panel is open: a change to
 * the session, which is what a line added to it is, on the canvas's stream, and a
 * gap in that stream, read what the session gained. A viewer of the chat gets the
 * change however the line came, from this browser, a teammate's, or the assistant.
 */
export function useLiveChat(sessionId: string | null): void {
  const hub = useContext(LiveContext);
  const client = useQueryClient();
  useEffect(() => {
    if (!hub || !sessionId) return;
    return hub.register({ update: (batch) => refresh(client, sessionId, batch.gap || batch.ids.has(sessionId)) }).dispose;
  }, [hub, client, sessionId]);
}

async function refresh(client: QueryClient, sessionId: string, due: boolean): Promise<null> {
  if (due) await refetchFresh(client, { queryKey: chatRecordsKey(sessionId), exact: true, type: "active" });
  return null;
}
