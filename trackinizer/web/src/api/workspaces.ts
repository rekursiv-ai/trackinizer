import { type CallOptions, client, send, TIMEOUT_MS } from "./client";
import type { components } from "./generated/schema";
import type { Provider } from "./providers";
import { valid, type CanvasState } from "./canvases";

export type Workspace = components["schemas"]["WorkspaceSummary"];
export type WorkspaceInfo = components["schemas"]["WorkspaceInfo"];
export type WorkspaceCanvas = components["schemas"]["WorkspaceCanvas"];
export type WorkspaceChanges = components["schemas"]["UpdateWorkspace"];

/** List the caller's workspaces in bar order, with optional search and hidden rows. */
export async function listWorkspaces(
  query: { readonly q?: string; readonly hidden?: boolean; readonly asker?: string } = {},
  { signal }: CallOptions = {},
): Promise<Workspace[]> {
  return send(TIMEOUT_MS.read, signal, (signal) => client.GET("/api/workspaces", {
    params: { query }, signal,
  }));
}

/** Create an owned workspace. */
export async function createWorkspace(title: string): Promise<Workspace> {
  return send(TIMEOUT_MS.write, undefined, (signal) => client.POST("/api/workspaces", {
    body: { title }, signal,
  }));
}

/** Rename a workspace or change its visibility or pin state. */
export async function updateWorkspace(id: string, changes: WorkspaceChanges): Promise<Workspace> {
  return send(TIMEOUT_MS.write, undefined, (signal) => client.PATCH("/api/workspaces/{workspace_id}", {
    params: { path: { workspace_id: id } }, body: changes, signal,
  }));
}

/** Save the chosen order for the caller's workspace bar. */
export async function setWorkspaceOrder(ids: readonly string[]): Promise<Workspace[]> {
  return send(TIMEOUT_MS.write, undefined, (signal) => client.PUT("/api/workspaces/order", {
    body: { ids: [...ids] }, signal,
  }));
}

/** Read one workspace and its attached canvases. */
export async function workspaceInfo(id: string, { signal }: CallOptions = {}): Promise<WorkspaceInfo> {
  return send(TIMEOUT_MS.read, signal, (signal) => client.GET("/api/workspaces/{workspace_id}", {
    params: { path: { workspace_id: id } }, signal,
  }));
}

/** Move a canvas to one of the caller's workspaces. */
export async function moveCanvas(canvasId: string, workspaceId: string): Promise<CanvasState> {
  return valid(await send(TIMEOUT_MS.write, undefined, (signal) => client.PUT("/api/canvases/{canvas_id}/workspace", {
    params: { path: { canvas_id: canvasId } }, body: { workspace_id: workspaceId }, signal,
  })));
}

/**
 * Create a canvas in a workspace, or in Scratch when no id is supplied. Its chat starts on `provider`, or on the
 * user's default when none is named.
 */
export async function createCanvas(workspaceId?: string, provider?: Provider): Promise<CanvasState> {
  const body = { ...(workspaceId ? { workspace_id: workspaceId } : {}), ...(provider ? { provider } : {}) };
  return valid(await send(TIMEOUT_MS.write, undefined, (signal) => client.POST("/api/canvases/new", {
    ...(Object.keys(body).length > 0 ? { body } : {}), signal,
  })));
}
