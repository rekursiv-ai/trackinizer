import { useSyncExternalStore } from "react";
import type { FocusRef } from "../router/route";
import { namedRows } from "../visuals/chatRefs";

/**
 * What the canvas is about, so the rail's Graph opens on it rather than bare.
 *
 * A detail page has had `Show in graph` all along (`detail/index.tsx`); this is the
 * same focus read off a composition instead of a row. A focus lights and does not
 * filter, so reading the subject wrongly costs the reader nothing they had.
 */
let topic: FocusRef | null = null;
const watchers = new Set<() => void>();

export function setTopic(next: FocusRef | null): void {
  if (sameTopic(topic, next)) return;
  topic = next;
  for (const watcher of [...watchers]) watcher();
}

export function currentTopic(): FocusRef | null {
  return topic;
}

export function useTopic(): FocusRef | null {
  return useSyncExternalStore(subscribe, currentTopic, nothing);
}

/**
 * The row `words` is about: the first it names, by the rule the reader sees.
 *
 * First rather than most-named — an answer leads with its subject, and counting would
 * let a tail of citations outvote the title. `namedRows` skips refs inside code.
 */
export function topicOf(words: string, kinds: readonly string[]): FocusRef | null {
  return namedRows(words, kinds)[0] ?? null;
}

/** How a focus on `ref` reads in a title. */
export function topicName(ref: FocusRef): string {
  return "id" in ref ? ref.id.slice(0, 8) : `${ref.kind}#${ref.seq}`;
}

export function sameTopic(a: FocusRef | null, b: FocusRef | null): boolean {
  if (a === null || b === null) return a === b;
  if ("id" in a || "id" in b) return "id" in a && "id" in b && a.id === b.id;
  return a.kind === b.kind && a.seq === b.seq;
}

function subscribe(watcher: () => void): () => void {
  watchers.add(watcher);
  return () => {
    watchers.delete(watcher);
  };
}

/** The server renders no canvas, so it is about nothing. */
function nothing(): null {
  return null;
}
