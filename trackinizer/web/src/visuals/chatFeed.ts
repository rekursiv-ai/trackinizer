import { createContext, useContext, useState, useSyncExternalStore } from "react";
import type { PendingLine } from "./chatLines";

/** An unconfirmed post's destination before its conversation has been assigned. */
export type PendingPost = PendingLine & { readonly canvasId: string };
type SavedPost = PendingPost & { readonly sender: string };

const PENDING_KEY = "trackinizer.chat.pending";

/** A request, from outside Chat, to open a science chat's session in it. */
export type ContinueRequest = { readonly sessionId: string; readonly n: number };

/** What Chat shares with the rest of the shell. */
export type ChatFeedState = {
  /** The newest request to continue a session in Chat, until Chat has taken it. */
  readonly request: ContinueRequest | null;
  /** Posts not yet confirmed by capture, scoped to their signed-in sender. */
  readonly pending: readonly SavedPost[];
};

/**
 * Requests to continue a session and posts awaiting capture live above Chat,
 * a lazy chunk that may mount late or remount. Captured conversation lines are
 * read through the query cache; the canvas owns its conversation's identity.
 */
export class ChatFeed {
  #state: ChatFeedState = { request: null, pending: readPending() };
  readonly #listeners = new Set<() => void>();
  #requests = 0;

  readonly subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => void this.#listeners.delete(listener);
  };

  readonly snapshot = (): ChatFeedState => this.#state;

  /** Ask Chat to open the science chat whose session is `sessionId`, when it is shown. */
  continueIn(sessionId: string): void {
    this.#requests += 1;
    this.#set({ ...this.#state, request: { sessionId, n: this.#requests } });
  }

  /** Chat took request `n`; a newer one that came meanwhile stays. */
  taken(n: number): void {
    if (this.#state.request?.n === n) this.#set({ ...this.#state, request: null });
  }

  /** Keep a sender's unconfirmed posts across Chat mounting and this tab reloading. */
  updatePending(sender: string, change: (held: readonly PendingPost[]) => readonly PendingPost[]): void {
    const held = this.#state.pending.filter((post) => post.sender === sender);
    const next = change(held);
    if (next === held) return;
    const pending = [...this.#state.pending.filter((post) => post.sender !== sender), ...next.map((post) => ({ ...post, sender }))];
    try {
      sessionStorage.setItem(PENDING_KEY, JSON.stringify(pending));
    } catch { /* The page still retains posts when browser storage is unavailable. */ }
    this.#set({ ...this.#state, pending });
  }

  #set(state: ChatFeedState): void {
    this.#state = state;
    for (const listener of [...this.#listeners]) listener();
  }
}

function readPending(): readonly SavedPost[] {
  try {
    const saved: unknown = JSON.parse(sessionStorage.getItem(PENDING_KEY) ?? "[]");
    if (!Array.isArray(saved)) return [];
    const posts: unknown[] = saved;
    return posts.filter(isPendingPost);
  } catch {
    return [];
  }
}

function isPendingPost(value: unknown): value is SavedPost {
  if (typeof value !== "object" || value === null) return false;
  const post = value as Record<string, unknown>;
  return typeof post.sender === "string" && post.sender.length > 0
    && typeof post.canvasId === "string" && post.canvasId.length > 0
    && typeof post.key === "string" && typeof post.text === "string"
    && (post.conversationId === null || typeof post.conversationId === "string")
    && typeof post.baseline === "number" && Number.isInteger(post.baseline) && post.baseline >= 0
    && (post.submittedAt === undefined || (typeof post.submittedAt === "number" && Number.isFinite(post.submittedAt)));
}

/** The shell's feed; null outside it. */
export const ChatFeedContext = createContext<ChatFeed | null>(null);

/** The feed Chat reads: the shell's, or one of its own where there is none. */
export function useChatFeed(): { readonly feed: ChatFeed; readonly state: ChatFeedState } {
  const provided = useContext(ChatFeedContext);
  const [own] = useState(() => new ChatFeed());
  const feed = provided ?? own;
  return { feed, state: useSyncExternalStore(feed.subscribe, feed.snapshot) };
}
