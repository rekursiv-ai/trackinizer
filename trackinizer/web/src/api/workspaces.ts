import { type CallOptions, client, send, TIMEOUT_MS } from "./client";
import type { components } from "./generated/schema";
import { type EventSourceLike, openEvents } from "./stream";

export type WorkspaceState = components["schemas"]["WorkspaceState"];
/** Who a canvas talks to: the assistant's newest live session. */
export type WorkspacePartner = NonNullable<WorkspaceState["partner"]>;
export type WorkspaceOperation = components["schemas"]["Operation"];

/** Create or reopen the signed-in user's default canvas. */
export async function createDefaultWorkspace({ signal }: CallOptions = {}): Promise<WorkspaceState> {
  return valid(await send(TIMEOUT_MS.write, signal, (signal) => client.POST("/api/workspaces", { signal })));
}

/** Read a revision for polling or after a conflict. */
export async function getWorkspace(id: string, { signal }: CallOptions = {}): Promise<WorkspaceState> {
  return valid(await send(TIMEOUT_MS.read, signal, (signal) => client.GET("/api/workspaces/{workspace_id}", {
    params: { path: { workspace_id: id } }, signal,
  })));
}

/** Apply one operation with a stable key so a network retry cannot duplicate it. */
export async function applyWorkspaceOperation(
  id: string,
  revision: number,
  operation: WorkspaceOperation,
  key: string,
): Promise<WorkspaceState> {
  return valid(await send(TIMEOUT_MS.write, undefined, (signal) => client.POST("/api/workspaces/{workspace_id}/operations", {
    params: { path: { workspace_id: id }, header: { "Idempotency-Key": key } },
    body: { revision, operation }, signal,
  })));
}

function valid(state: WorkspaceState): WorkspaceState {
  if (!state || typeof state.id !== "string" || !Number.isInteger(state.revision)
    || !Array.isArray(state.visuals)
    || !state.visuals.every((visual) => visual && typeof visual.id === "string" && typeof visual.type === "string")) {
    throw new Error("Invalid workspace state");
  }
  return state;
}

/** What the workspace's events stream tells its listener; `t` is the server's epoch milliseconds. */
export type WorkspaceEventListener = {
  /** The canvas changed, or its first state after the stream opened; `shown` is the instance an agent's show brought up, else null. */
  readonly workspace: (state: WorkspaceState, t: number, shown: string | null) => void;
  /** An agent moved the page: `route` is a `#/...` hash, not yet checked against the router. */
  readonly navigate: (route: string, t: number) => void;
  /** An agent pointed at inquiries, by id; the newest list replaces the last and an empty one clears. */
  readonly highlight: (ids: readonly string[], t: number) => void;
  /** An inquiry changed, as `/api/web/subscribe` says it; a chat's new line is its session's change. */
  readonly changed: (id: string, t: number) => void;
  /** The stream is connected: the first time, and after every reconnect. */
  readonly open: () => void;
  /** The stream dropped; it reconnects on its own. */
  readonly drop: () => void;
  /** The server refused the stream; the wrapper tries again later. */
  readonly refuse: () => void;
};

/**
 * Listen to `GET /api/workspaces/{id}/events`: the tab's one stream, one
 * `EventSource` that reconnects as `openStream` does (the server starts each
 * connection with a `workspace` frame). A frame of another shape is logged and
 * skipped. Returns the function that closes it.
 */
export function openWorkspaceEvents(
  workspaceId: string,
  listener: WorkspaceEventListener,
  options: { readonly connect?: (url: string) => EventSourceLike; readonly retryDelaysMs?: readonly number[] } = {},
): () => void {
  return openEvents(`/api/workspaces/${encodeURIComponent(workspaceId)}/events`, {
    open: listener.open,
    drop: listener.drop,
    refuse: listener.refuse,
    data: (data) => {
      const frame = parseFrame(data);
      if (!frame) {
        console.warn("Ignored a workspace frame of an unknown shape.", data);
        return;
      }
      switch (frame.type) {
        case "workspace": return listener.workspace(frame.state, frame.t, frame.shown);
        case "navigate": return listener.navigate(frame.route, frame.t);
        case "highlight": return listener.highlight(frame.ids, frame.t);
        case "changed": return listener.changed(frame.id, frame.t);
      }
    },
  }, options);
}

type Frame =
  | { readonly type: "workspace"; readonly state: WorkspaceState; readonly shown: string | null; readonly t: number }
  | { readonly type: "navigate"; readonly route: string; readonly t: number }
  | { readonly type: "highlight"; readonly ids: readonly string[]; readonly t: number }
  | { readonly type: "changed"; readonly id: string; readonly t: number };

/** One frame's data as a checked `Frame`, or null. */
function parseFrame(data: unknown): Frame | null {
  if (typeof data !== "string") return null;
  let frame: unknown;
  try {
    frame = JSON.parse(data);
  } catch {
    return null;
  }
  if (typeof frame !== "object" || frame === null) return null;
  const { type, t, state, shown, route, id, ids } = frame as { [name: string]: unknown };
  if (typeof t !== "number") return null;
  if (type === "workspace") {
    try {
      return { type, state: valid(state as WorkspaceState), shown: typeof shown === "string" ? shown : null, t };
    } catch {
      return null;
    }
  }
  if (type === "navigate") return typeof route === "string" ? { type, route, t } : null;
  if (type === "highlight") return Array.isArray(ids) && ids.every((each) => typeof each === "string") ? { type, ids, t } : null;
  if (type === "changed") return typeof id === "string" ? { type, id, t } : null;
  return null;
}
