// What a station's code is filed against.
//
// The binding the feature is for is PER SESSION: a research session and the code it is about
// are one thing, and chaining each session's snapshot onto the last is what makes "what did
// the agent change this session" answerable at all.
//
// But a station has no session until a chat starts, and a pane that can only say "no session
// yet" is dead before the first message — there is nowhere to drop a folder. So the fall-back
// is the WORKSTREAM, which outlives every session under it. A session that starts later files
// its own snapshot naming the workstream's as parent, so the fall-back is the beginning of the
// chain rather than a detour around it.
//
// The scope is named in the UI because the two are not interchangeable: code filed on a
// workstream outlives the chat, and a reader must know which they are looking at.
import type { WorkspaceState } from "../api/workspaces";

/** Which record a station's code is filed against, and what that record is. */
export type CodeScope = {
  readonly record: string;
  readonly kind: "session" | "workstream";
};

/**
 * The record `station`'s code belongs to: its live chat run's session, else the station's own
 * session, else the workstream it sits in. Null when the station names none of the three,
 * which is the only case with nowhere to file code.
 */
export function codeScope(station: WorkspaceState | undefined): CodeScope | null {
  if (!station) return null;
  // The live run's session first: it is the conversation actually happening, which the
  // station's own `session_id` can lag behind.
  const session = station.partner?.session_id ?? station.session_id ?? null;
  if (session) return { record: session, kind: "session" };
  return station.workstream_id ? { record: station.workstream_id, kind: "workstream" } : null;
}

/** What the scope is called where a reader sees it. */
export function scopeName(kind: CodeScope["kind"]): string {
  return kind === "session" ? "this session" : "this workstream";
}
