import { createContext, useContext, useState, useSyncExternalStore } from "react";

/** What the events stream has told Chat about each conversation, by id. */
export type ChatFeedState = {
  /** How many times the stream has opened; a rise after the first means a gap. */
  readonly opens: number;
  /**
   * The partner's status line: `""` once it cleared one, absent before any and
   * after a new message.
   */
  readonly status: Readonly<Record<string, string>>;
  /** The highest `seq` the partner has drained, as the last `delivered` frame said. */
  readonly delivered: Readonly<Record<string, number>>;
  /** Conversations deleted since the page opened, here or in another tab. */
  readonly deleted: Readonly<Record<string, true>>;
  /** Bumped when a canvas's open conversation changes. */
  readonly openVersion: number;
};

/**
 * Chat's state that is not a conversation's lines (those live in the query
 * cache, `chatCache.ts`): the partner's status, delivery, the stream's opens,
 * and which conversation each canvas has open. It lives above Chat, which is a
 * lazy chunk that may mount late and remount, so none of it is lost.
 */
export class ChatFeed {
  #state: ChatFeedState = { opens: 0, status: {}, delivered: {}, deleted: {}, openVersion: 0 };
  readonly #open = new Map<string, string | null>();
  readonly #listeners = new Set<() => void>();

  readonly subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => void this.#listeners.delete(listener);
  };

  readonly snapshot = (): ChatFeedState => this.#state;

  /** The stream connected. */
  opened(): void {
    this.#set({ ...this.#state, opens: this.#state.opens + 1 });
  }

  /** A message was stored in a conversation: its status starts over. */
  messaged(conversationId: string): void {
    if (!(conversationId in this.#state.status)) return;
    const { [conversationId]: _, ...status } = this.#state.status;
    this.#set({ ...this.#state, status });
  }

  /** The partner's status for a conversation; `""` clears it, and the panel then shows none. */
  setStatus(conversationId: string, text: string): void {
    this.#set({ ...this.#state, status: { ...this.#state.status, [conversationId]: text } });
  }

  /** The partner drained the conversation's messages through `seq`. */
  drained(conversationId: string, seq: number): void {
    if (seq <= (this.#state.delivered[conversationId] ?? 0)) return;
    this.#set({ ...this.#state, delivered: { ...this.#state.delivered, [conversationId]: seq } });
  }

  /** Forget a conversation, once it is deleted; `gone` says so to a Chat that has it open. */
  forget(conversationId: string, gone = false): void {
    const { [conversationId]: _s, ...status } = this.#state.status;
    const { [conversationId]: _d, ...delivered } = this.#state.delivered;
    this.#set({
      ...this.#state, status, delivered,
      deleted: gone ? { ...this.#state.deleted, [conversationId]: true } : this.#state.deleted,
    });
  }

  /**
   * The conversation open on `workspaceId`: what was set, else what
   * `localStorage` kept from an earlier page, else null. It is held here, so
   * Chat remounting, or storage refused, loses nothing.
   */
  openId(workspaceId: string | null): string | null {
    if (!workspaceId) return null;
    if (!this.#open.has(workspaceId)) this.#open.set(workspaceId, readStored(workspaceId));
    return this.#open.get(workspaceId) ?? null;
  }

  /** Open a conversation on `workspaceId`; null opens a new chat. */
  setOpen(workspaceId: string, id: string | null): void {
    this.#open.set(workspaceId, id);
    writeStored(workspaceId, id);
    this.#set({ ...this.#state, openVersion: this.#state.openVersion + 1 });
  }

  #set(state: ChatFeedState): void {
    this.#state = state;
    for (const listener of [...this.#listeners]) listener();
  }
}

/** The shell's feed; null outside it. */
export const ChatFeedContext = createContext<ChatFeed | null>(null);

/** The feed Chat reads: the shell's, or one of its own where there is none. */
export function useChatFeed(): { readonly feed: ChatFeed; readonly state: ChatFeedState } {
  const provided = useContext(ChatFeedContext);
  const [own] = useState(() => (provided ? null : new ChatFeed()));
  const feed = provided ?? own!;
  return { feed, state: useSyncExternalStore(feed.subscribe, feed.snapshot) };
}

const storedKey = (workspaceId: string) => `trackinizer.v2.chat.${workspaceId}`;

function readStored(workspaceId: string): string | null {
  try {
    return localStorage.getItem(storedKey(workspaceId));
  } catch {
    return null;
  }
}

function writeStored(workspaceId: string, id: string | null): void {
  try {
    if (id) localStorage.setItem(storedKey(workspaceId), id);
    else localStorage.removeItem(storedKey(workspaceId));
  } catch {
    // Storage is off: the feed still holds it until the page goes.
  }
}
