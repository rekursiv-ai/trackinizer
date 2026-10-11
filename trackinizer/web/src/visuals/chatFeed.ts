import { createContext, useContext, useState, useSyncExternalStore } from "react";

/** A request, from outside Chat, to open a science chat's session in it. */
export type ContinueRequest = { readonly sessionId: string; readonly n: number };

/** What Chat shares with the rest of the shell. */
export type ChatFeedState = {
  /** The newest request to continue a session in Chat, until Chat has taken it. */
  readonly request: ContinueRequest | null;
};

/**
 * Chat's state that is not a conversation's lines (those are its session's records,
 * read through the query cache; which conversation a station holds is the station's):
 * a request from the Console or a session's page to continue a session in Chat. It
 * lives above Chat, which is a lazy chunk that may mount late and remount, so none
 * of it is lost.
 */
export class ChatFeed {
  #state: ChatFeedState = { request: null };
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
  const [own] = useState(() => new ChatFeed());
  const feed = provided ?? own;
  return { feed, state: useSyncExternalStore(feed.subscribe, feed.snapshot) };
}
