import { type CallOptions, client, send, TIMEOUT_MS } from "./client";
import type { components } from "./generated/schema";

export type WorkspaceState = components["schemas"]["WorkspaceState"];
export type WorkspaceOperation = components["schemas"]["Operation"];
export type ConnectableSession = components["schemas"]["ConnectableSession"];
export type WorkspaceConnectionStatus = components["schemas"]["WorkspaceConnectionStatus"];
export type WorkspaceMessageReceipt = components["schemas"]["WorkspaceMessageReceipt"];

/** Read the signed-in account's live sessions that may be paired. */
export async function listConnectableSessions({ signal }: CallOptions = {}): Promise<ConnectableSession[]> {
  const sessions = await send(TIMEOUT_MS.read, signal, (signal) =>
    client.GET("/api/workspaces/sessions/connectable", { signal }));
  if (!Array.isArray(sessions) || !sessions.every((session) =>
    session && typeof session.id === "string" && typeof session.title === "string" && typeof session.actor === "string")) {
    throw new Error("Invalid connectable sessions");
  }
  return sessions;
}

/** Check the stored pairing directly; the recent-session picker is capped. */
export async function getWorkspaceConnectionStatus(
  id: string,
  { signal }: CallOptions = {},
): Promise<WorkspaceConnectionStatus> {
  return send(TIMEOUT_MS.read, signal, (signal) => client.GET("/api/workspaces/{workspace_id}/connection", {
    params: { path: { workspace_id: id } }, signal,
  }));
}

/** Queue a message for the live session paired to this canvas. */
export async function sendWorkspaceMessage(
  id: string,
  text: string,
  chatInstanceId: string | null,
  expectedRecordId: string | null,
  key: string,
): Promise<WorkspaceMessageReceipt> {
  return send(TIMEOUT_MS.write, undefined, (signal) => client.POST("/api/workspaces/{workspace_id}/messages", {
    params: { path: { workspace_id: id }, header: { "Idempotency-Key": key } },
    body: { text, chat_instance_id: chatInstanceId, expected_record_id: expectedRecordId }, signal,
  }));
}

/** Connect a live AgentSession to the canvas, or disconnect with null. */
export async function setWorkspaceConnection(
  id: string,
  revision: number,
  sessionId: string | null,
): Promise<WorkspaceState> {
  return valid(await send(TIMEOUT_MS.write, undefined, (signal) => client.PUT("/api/workspaces/{workspace_id}/connection", {
    params: { path: { workspace_id: id } }, body: { revision, session_id: sessionId }, signal,
  })));
}

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
