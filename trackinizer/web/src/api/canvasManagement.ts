import { type CallOptions, client, send, TIMEOUT_MS } from "./client";
import type { components } from "./generated/schema";
import type { Provider } from "./providers";
import { valid, type CanvasState } from "./canvases";

/**
 * One canvas in the user's list: its name, its chat's conversation and the page it last showed, and what its
 * chat's run is doing (`state`: waiting on the person, or taken over in a terminal).
 */
export type CanvasSummary = components["schemas"]["CanvasSummary"];

/** The user's canvases, the one changed last first. */
export async function listCanvases({ signal }: CallOptions = {}): Promise<CanvasSummary[]> {
  const canvases = await send(TIMEOUT_MS.read, signal, (signal) => client.GET("/api/canvases", { signal }));
  if (!Array.isArray(canvases) || !canvases.every((each) => typeof each?.id === "string" && typeof each.name === "string")) {
    throw new Error("Invalid canvas list");
  }
  return canvases;
}

/**
 * Make a canvas, named `unnamed-canvas#N`, and open it. Its chat starts on `provider`, or on the user's
 * default when none is named.
 */
export async function createCanvas({ signal, provider }: CallOptions & { readonly provider?: Provider } = {}): Promise<CanvasState> {
  return valid(await send(TIMEOUT_MS.write, signal, (signal) => client.POST("/api/canvases/new", {
    ...(provider ? { body: { provider } } : {}), signal,
  })));
}

/** Rename a canvas; a name the user already has is a 409. */
export async function renameCanvas(id: string, name: string): Promise<CanvasState> {
  return valid(await send(TIMEOUT_MS.write, undefined, (signal) => client.PATCH("/api/canvases/{canvas_id}", {
    params: { path: { canvas_id: id } }, body: { name }, signal,
  })));
}

/** Remember the page a canvas shows. It changes no revision and publishes nothing. */
export async function setCanvasPage(id: string, route: string): Promise<void> {
  await send(TIMEOUT_MS.write, undefined, (signal) => client.PUT("/api/canvases/{canvas_id}/page", {
    params: { path: { canvas_id: id } }, body: { route }, signal,
  }));
}

/**
 * Remember how a canvas's columns and windows were left, as fractions of the
 * stage (Chat's width in `ch`). Each key given replaces the stored one; the rest
 * stay. It changes no revision and publishes nothing.
 */
export async function setCanvasLayout(id: string, layout: components["schemas"]["CanvasLayout"]): Promise<void> {
  await send(TIMEOUT_MS.write, undefined, (signal) => client.PUT("/api/canvases/{canvas_id}/layout", {
    params: { path: { canvas_id: id } }, body: layout, signal,
  }));
}

/** Get or create the caller's personal settings Artifact. */
export async function openPersonalSettings(): Promise<string> {
  const settings = await send(TIMEOUT_MS.write, undefined, (signal) => client.POST("/api/me/settings-artifact", { signal }));
  if (!settings || typeof settings.artifact_id !== "string") throw new Error("Invalid settings artifact");
  return settings.artifact_id;
}

/** Open the user's canvas that holds a chat, or make one bound to it. */
export async function canvasForChat(conversationId: string): Promise<CanvasState> {
  return valid(await send(TIMEOUT_MS.write, undefined, (signal) => client.POST("/api/canvases/for-chat/{conversation_id}", {
    params: { path: { conversation_id: conversationId } }, signal,
  })));
}

/** Hide or restore one of the user's canvases in the bar. */
export async function hideCanvas(id: string, hidden = true): Promise<CanvasState> {
  return valid(await send(TIMEOUT_MS.write, undefined, (signal) => client.PATCH("/api/canvases/{canvas_id}", {
    params: { path: { canvas_id: id } }, body: { hidden }, signal,
  })));
}

/** Pin or unpin one of the user's canvases. */
export async function pinCanvas(id: string, pinned: boolean): Promise<CanvasState> {
  return valid(await send(TIMEOUT_MS.write, undefined, (signal) => client.PATCH("/api/canvases/{canvas_id}", {
    params: { path: { canvas_id: id } }, body: { pinned }, signal,
  })));
}

/** Clear a canvas's chat and return the server-created canvas with its copied canvas. */
export async function clearChat(id: string, key: string): Promise<CanvasState> {
  return valid(await send(TIMEOUT_MS.write, undefined, (signal) => client.POST("/api/canvases/{canvas_id}/clear-chat", {
    params: { path: { canvas_id: id }, header: { "Idempotency-Key": key } }, signal,
  })));
}

/** The Console agents a line was sent to directly, as the relay names them. */
export type RelayTarget = { readonly actor: string; readonly room: string | null };

/**
 * Tell a canvas's chat what the user said to agents from the Console: the chat
 * records one line for it and its partner hears it. False (the canvas has no chat,
 * its partner is down) is no failure. A retry under the same key tells it once.
 */
export async function relayToCanvas(id: string, targets: readonly RelayTarget[], text: string, key: string): Promise<boolean> {
  const told = await send(TIMEOUT_MS.write, undefined, (signal) => client.POST("/api/canvases/{canvas_id}/relay", {
    params: { path: { canvas_id: id }, header: { "Idempotency-Key": key } },
    body: { targets: targets.map(({ actor, room }) => ({ actor, room })), text }, signal,
  }));
  return told.delivered;
}
