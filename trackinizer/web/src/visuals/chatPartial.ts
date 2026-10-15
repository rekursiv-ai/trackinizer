import { useEffect, useState } from "react";
import { type ChatPartial, readChatPartial } from "../api/chats";
import { ApiError } from "../api/client";

/** The reply the assistant is writing, as Chat shows it: its turn and its text so far. */
export type ChatBubble = { readonly turn: string; readonly text: string };

/** How long Chat waits to read a chat's partial again after a failed read, by how many failed in a row. */
const RETRY_MS = [1_000, 2_000, 5_000] as const;

/**
 * The bubble Chat shows once the server holds `partial`, from the one `shown` and the
 * turn whose answer is stored, `spent`. No partial, or the spent turn's, shows none. A
 * finished turn's text (`done`) shows only where its bubble already is, so a leftover
 * answer never raises a bubble under a new line. Any other partial shows its text.
 */
export function nextBubble(shown: ChatBubble | null, partial: ChatPartial | null, spent: string | null): ChatBubble | null {
  if (partial === null || partial.turn === spent) return null;
  if (partial.done && shown?.turn !== partial.turn) return null;
  return { turn: partial.turn, text: partial.text };
}

/**
 * The reply the assistant is writing in a chat's session, while `active` (an answer is
 * owed in a chat the reader reads, and its partner is live). Each read is held by the
 * server until the partial changes, and the next asks after the `seq` it answered;
 * what it holds goes through `nextBubble`. When the count of stored `answers` changes,
 * the turn shown is spent and its bubble goes in that same render, so the answer
 * takes its place with no frame holding both. A 404 (the server streams no replies)
 * ends the reads; another failure reads again after 1 s, then 2 s, then every 5 s.
 * Going idle, another session, or unmounting aborts the read under way.
 */
export function useChatPartial(
  sessionId: string | null,
  { active, answers }: { readonly active: boolean; readonly answers: number },
): ChatBubble | null {
  const [held, setHeld] = useState<Held>(() => ({ sessionId, answers, bubble: null, spent: null }));
  // Adjusted while rendering, not in an effect, which would run after a frame showing both.
  const now: Held = held.sessionId !== sessionId ? { sessionId, answers, bubble: null, spent: null }
    : held.answers !== answers ? { sessionId, answers, bubble: null, spent: held.bubble?.turn ?? null }
      : held;
  if (now !== held) setHeld(now);
  useEffect(() => {
    if (!active || sessionId === null) return;
    const abort = new AbortController();
    void follow(sessionId, abort.signal, (partial) => setHeld((latest) =>
      // A read of the session before can land after this render moved on, before its abort.
      latest.sessionId === sessionId ? { ...latest, bubble: nextBubble(latest.bubble, partial, latest.spent) } : latest));
    return () => abort.abort();
  }, [active, sessionId]);
  return active ? now.bubble : null;
}

/** What the hook holds for one session: the answers it last counted, the bubble shown, and the spent turn. */
type Held = {
  readonly sessionId: string | null;
  readonly answers: number;
  readonly bubble: ChatBubble | null;
  /** The turn whose bubble an arriving answer took the place of: a partial of it is a leftover. */
  readonly spent: string | null;
};

/** Read a session's partial until `signal` aborts or the server says 404, handing each to `take`. */
async function follow(sessionId: string, signal: AbortSignal, take: (partial: ChatPartial | null) => void): Promise<void> {
  let after = 0;
  let failures = 0;
  while (!signal.aborted) {
    try {
      const state = await readChatPartial(sessionId, after, { signal });
      if (signal.aborted) return;
      after = state.seq;
      failures = 0;
      take(state.partial);
    } catch (error) {
      if (signal.aborted || (error instanceof ApiError && error.status === 404)) return;
      await pause(RETRY_MS[Math.min(failures, RETRY_MS.length - 1)]!, signal);
      failures += 1;
    }
  }
}

/** Resolve after `ms`, or at once when `signal` aborts. */
function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const stop = () => {
      clearTimeout(timer);
      resolve();
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", stop);
      resolve();
    }, ms);
    signal.addEventListener("abort", stop, { once: true });
  });
}
