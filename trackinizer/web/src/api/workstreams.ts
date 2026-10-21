import { type CallOptions, client, send, TIMEOUT_MS } from "./client";
import type { components } from "./generated/schema";
import type { Provider } from "./providers";
import { valid, type WorkspaceState } from "./workspaces";

export type Workstream = components["schemas"]["WorkstreamSummary"];
export type WorkstreamInfo = components["schemas"]["WorkstreamInfo"];
export type WorkstreamStation = components["schemas"]["WorkstreamStation"];
export type WorkstreamChanges = components["schemas"]["UpdateWorkstream"];

/** List the caller's workstreams in bar order, with optional search and hidden rows. */
export async function listWorkstreams(
  query: { readonly q?: string; readonly hidden?: boolean; readonly asker?: string } = {},
  { signal }: CallOptions = {},
): Promise<Workstream[]> {
  return send(TIMEOUT_MS.read, signal, (signal) => client.GET("/api/workstreams", {
    params: { query }, signal,
  }));
}

/** Create an owned workstream. */
export async function createWorkstream(title: string): Promise<Workstream> {
  return send(TIMEOUT_MS.write, undefined, (signal) => client.POST("/api/workstreams", {
    body: { title }, signal,
  }));
}

/** Rename a workstream or change its visibility or pin state. */
export async function updateWorkstream(id: string, changes: WorkstreamChanges): Promise<Workstream> {
  return send(TIMEOUT_MS.write, undefined, (signal) => client.PATCH("/api/workstreams/{workstream_id}", {
    params: { path: { workstream_id: id } }, body: changes, signal,
  }));
}

/** Save the chosen order for the caller's workstream bar. */
export async function setWorkstreamOrder(ids: readonly string[]): Promise<Workstream[]> {
  return send(TIMEOUT_MS.write, undefined, (signal) => client.PUT("/api/workstreams/order", {
    body: { ids: [...ids] }, signal,
  }));
}

/** Read one workstream and its attached stations. */
export async function workstreamInfo(id: string, { signal }: CallOptions = {}): Promise<WorkstreamInfo> {
  return send(TIMEOUT_MS.read, signal, (signal) => client.GET("/api/workstreams/{workstream_id}", {
    params: { path: { workstream_id: id } }, signal,
  }));
}

/** Move a station to one of the caller's workstreams. */
export async function moveStation(workspaceId: string, workstreamId: string): Promise<WorkspaceState> {
  return valid(await send(TIMEOUT_MS.write, undefined, (signal) => client.PUT("/api/workspaces/{workspace_id}/workstream", {
    params: { path: { workspace_id: workspaceId } }, body: { workstream_id: workstreamId }, signal,
  })));
}

/**
 * Create a station in a workstream, or in Scratch when no id is supplied. Its chat starts on `provider`, or on the
 * user's default when none is named.
 */
export async function createStation(workstreamId?: string, provider?: Provider): Promise<WorkspaceState> {
  const body = { ...(workstreamId ? { workstream_id: workstreamId } : {}), ...(provider ? { provider } : {}) };
  return valid(await send(TIMEOUT_MS.write, undefined, (signal) => client.POST("/api/workspaces/new", {
    ...(Object.keys(body).length > 0 ? { body } : {}), signal,
  })));
}
