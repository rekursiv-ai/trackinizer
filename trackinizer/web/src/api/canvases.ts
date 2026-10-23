import { type CallOptions, client, send, TIMEOUT_MS } from "./client";
import { newUuid } from "./idempotency";
import type { components } from "./generated/schema";
import type { Provider } from "./providers";
import { type EventSourceLike, openEvents } from "./stream";

/** The user's preferences as a frame carries them: every key, defaults filled (`src/preferences.ts`). */
type Preferences = Required<components["schemas"]["Preferences"]>;

export type CanvasState = components["schemas"]["CanvasState"];
/**
 * Who a canvas's Chat talks to: the provider's live run, or the assistant. `status` is `offline` for a CLI whose
 * chat has no live run; `label` reads as `claude · gx10`.
 */
export type CanvasPartner = NonNullable<CanvasState["partner"]>;
export type CanvasOperation = components["schemas"]["Operation"];
export type CampaignSetup = components["schemas"]["CampaignSetup"];
export type SessionCampaign = components["schemas"]["SessionCampaign"];

/** The canvas the signed-in user changed last, or their first, made if there is none. */
export async function createDefaultCanvas({ signal }: CallOptions = {}): Promise<CanvasState> {
  return valid(await send(TIMEOUT_MS.write, signal, (signal) => client.POST("/api/canvases", { signal })));
}

/** Read a revision for polling or after a conflict. */
export async function getCanvas(id: string, { signal }: CallOptions = {}): Promise<CanvasState> {
  return valid(await send(TIMEOUT_MS.read, signal, (signal) => client.GET("/api/canvases/{canvas_id}", {
    params: { path: { canvas_id: id } }, signal,
  })));
}

/** Apply one operation with a stable key so a network retry cannot duplicate it. */
export async function applyCanvasOperation(
  id: string,
  revision: number,
  operation: CanvasOperation,
  key: string,
): Promise<CanvasState> {
  return valid(await send(TIMEOUT_MS.write, undefined, (signal) => client.POST("/api/canvases/{canvas_id}/operations", {
    params: { path: { canvas_id: id }, header: { "Idempotency-Key": key } },
    body: { revision, operation }, signal,
  })));
}

/**
 * Choose the provider the canvas's Chat talks to, at `revision`. The choice starts no run: a CLI's run is
 * started on a machine after it. Browser-only (an API key is refused). Choosing an unavailable provider is a 409
 * `provider_unavailable`, with its reason as the detail. A choice does not interrupt an assistant turn already in
 * flight; that turn's answer still lands.
 */
export async function chooseProvider(id: string, revision: number, provider: Provider, key: string): Promise<CanvasState> {
  return valid(await send(TIMEOUT_MS.write, undefined, (signal) => client.POST("/api/canvases/{canvas_id}/operations", {
    params: { path: { canvas_id: id }, header: { "Idempotency-Key": key } },
    body: { revision, operation: { kind: "provider", provider } }, signal,
  })));
}

export type Mark = components["schemas"]["Mark"];
export type NewMark = components["schemas"]["NewMark"];

/** Draw one red box. Boxes commute with every other write, so no revision is expected. */
export function drawMark(id: string, mark: NewMark): Promise<CanvasState> {
  return applyCanvasOperation(id, 0, { kind: "mark", mark }, newUuid());
}

/** Take one box away, whoever drew it; one already gone is no change. */
export function removeMark(id: string, markId: string): Promise<CanvasState> {
  return applyCanvasOperation(id, 0, { kind: "remove_mark", id: markId }, newUuid());
}

/** Take away the boxes shown on `page` (those with no page too), as the pen's page sees them. */
export function clearMarks(id: string, page: string): Promise<CanvasState> {
  return applyCanvasOperation(id, 0, { kind: "clear_marks", page }, newUuid());
}

export type CaptureImage = components["schemas"]["CaptureImage"];
/** A page's reply to an agent's request for a picture of the canvas: the picture, or why none. */
export type CaptureAnswer = { readonly image: CaptureImage } | { readonly error: string };

/** Answer request `request` for a picture of canvas `id`; one already answered is a 404. */
export async function answerCapture(id: string, request: string, answer: CaptureAnswer): Promise<void> {
  await send(TIMEOUT_MS.write, undefined, (signal) => client.PUT("/api/canvases/{canvas_id}/captures/{capture_id}", {
    params: { path: { canvas_id: id, capture_id: request } }, body: answer, signal,
  }));
}

/** Store or clear campaign defaults on the current chat-head session. */
export async function setSessionSettings(canvasId: string, campaign: components["schemas"]["CampaignSetup"] | null): Promise<SessionCampaign> {
  return send(TIMEOUT_MS.write, undefined, (signal) => client.PUT("/api/canvases/{canvas_id}/session-settings", {
    params: { path: { canvas_id: canvasId } }, body: { campaign }, signal,
  }));
}

export function valid(state: CanvasState): CanvasState {
  if (!state || typeof state.id !== "string" || !Number.isInteger(state.revision)
    || !Array.isArray(state.visuals)
    || !state.visuals.every((visual) => visual && typeof visual.id === "string" && typeof visual.type === "string")) {
    throw new Error("Invalid canvas state");
  }
  return state;
}

/** What the canvas's events stream tells its listener; `t` is the server's epoch milliseconds. */
export type CanvasEventListener = {
  /** The canvas changed, or its first state after the stream opened. */
  readonly canvas: (state: CanvasState, t: number) => void;
  /** An agent moved the page: `route` is a `#/...` hash, not yet checked against the router. */
  readonly navigate: (route: string, t: number) => void;
  /** An agent pointed at inquiries, by id; the newest list replaces the last and an empty one clears. */
  readonly highlight: (ids: readonly string[], t: number) => void;
  /** An agent asks this tab for a picture of the canvas; `request` is the id to answer. Not replayed. */
  readonly capture: (request: string, t: number) => void;
  /** An agent hid Chat's column (`true`) or showed it again (`false`); this tab's state, not the canvas's. */
  readonly chat: (hidden: boolean, t: number) => void;
  /** The user's preferences changed, by them on another tab or by the canvas's agent: every key. */
  readonly preferences: (preferences: Preferences, t: number) => void;
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
 * Listen to `GET /api/canvases/{id}/events`: the tab's one stream, one
 * `EventSource` that reconnects as `openStream` does (the server starts each
 * connection with a `canvas` frame). A frame of another shape is logged and
 * skipped. Returns the function that closes it.
 */
export function openCanvasEvents(
  canvasId: string,
  listener: CanvasEventListener,
  options: { readonly connect?: (url: string) => EventSourceLike; readonly retryDelaysMs?: readonly number[] } = {},
): () => void {
  return openEvents(`/api/canvases/${encodeURIComponent(canvasId)}/events`, {
    open: listener.open,
    drop: listener.drop,
    refuse: listener.refuse,
    data: (data) => {
      const frame = parseFrame(data);
      if (!frame) {
        console.warn("Ignored a canvas frame of an unknown shape.", data);
        return;
      }
      switch (frame.type) {
        case "canvas": return listener.canvas(frame.state, frame.t);
        case "navigate": return listener.navigate(frame.route, frame.t);
        case "highlight": return listener.highlight(frame.ids, frame.t);
        case "chat": return listener.chat(frame.hidden, frame.t);
        case "capture": return listener.capture(frame.request, frame.t);
        case "preferences": return listener.preferences(frame.preferences, frame.t);
        case "changed": return listener.changed(frame.id, frame.t);
      }
    },
  }, options);
}

type Frame =
  | { readonly type: "canvas"; readonly state: CanvasState; readonly t: number }
  | { readonly type: "navigate"; readonly route: string; readonly t: number }
  | { readonly type: "highlight"; readonly ids: readonly string[]; readonly t: number }
  | { readonly type: "chat"; readonly hidden: boolean; readonly t: number }
  | { readonly type: "capture"; readonly request: string; readonly t: number }
  | { readonly type: "preferences"; readonly preferences: Preferences; readonly t: number }
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
  const { type, t, state, route, id, ids, hidden, preferences, request } = frame as { [name: string]: unknown };
  if (typeof t !== "number") return null;
  if (type === "canvas") {
    try {
      return { type, state: valid(state as CanvasState), t };
    } catch {
      return null;
    }
  }
  if (type === "navigate") return typeof route === "string" ? { type, route, t } : null;
  if (type === "highlight") return Array.isArray(ids) && ids.every((each) => typeof each === "string") ? { type, ids, t } : null;
  if (type === "chat") return typeof hidden === "boolean" ? { type, hidden, t } : null;
  if (type === "capture") return typeof request === "string" ? { type, request, t } : null;
  // The server sends every key; the store takes them as they come.
  if (type === "preferences") return typeof preferences === "object" && preferences !== null ? { type, preferences: preferences as Preferences, t } : null;
  if (type === "changed") return typeof id === "string" ? { type, id, t } : null;
  return null;
}
