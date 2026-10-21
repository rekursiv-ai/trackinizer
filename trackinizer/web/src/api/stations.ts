import { type CallOptions, client, send, TIMEOUT_MS } from "./client";
import type { components } from "./generated/schema";
import type { Provider } from "./providers";
import { valid, type WorkspaceState } from "./workspaces";

/** One station in the user's list: its name, its chat's conversation and the page it last showed. */
export type StationSummary = components["schemas"]["WorkspaceSummary"];

/** The user's stations, the one changed last first. */
export async function listStations({ signal }: CallOptions = {}): Promise<StationSummary[]> {
  const stations = await send(TIMEOUT_MS.read, signal, (signal) => client.GET("/api/workspaces", { signal }));
  if (!Array.isArray(stations) || !stations.every((each) => typeof each?.id === "string" && typeof each.name === "string")) {
    throw new Error("Invalid station list");
  }
  return stations;
}

/**
 * Make a station, named `unnamed-workstream#N`, and open it. Its chat starts on `provider`, or on the user's
 * default when none is named.
 */
export async function createStation({ signal, provider }: CallOptions & { readonly provider?: Provider } = {}): Promise<WorkspaceState> {
  return valid(await send(TIMEOUT_MS.write, signal, (signal) => client.POST("/api/workspaces/new", {
    ...(provider ? { body: { provider } } : {}), signal,
  })));
}

/** Rename a station; a name the user already has is a 409. */
export async function renameStation(id: string, name: string): Promise<WorkspaceState> {
  return valid(await send(TIMEOUT_MS.write, undefined, (signal) => client.PATCH("/api/workspaces/{workspace_id}", {
    params: { path: { workspace_id: id } }, body: { name }, signal,
  })));
}

/** Remember the page a station shows. It changes no revision and publishes nothing. */
export async function setStationPage(id: string, route: string): Promise<void> {
  await send(TIMEOUT_MS.write, undefined, (signal) => client.PUT("/api/workspaces/{workspace_id}/page", {
    params: { path: { workspace_id: id } }, body: { route }, signal,
  }));
}

/**
 * Remember how a station's columns and windows were left, as fractions of the
 * stage (Chat's width in `ch`). Each key given replaces the stored one; the rest
 * stay. It changes no revision and publishes nothing.
 */
export async function setStationLayout(id: string, layout: components["schemas"]["StationLayout"]): Promise<void> {
  await send(TIMEOUT_MS.write, undefined, (signal) => client.PUT("/api/workspaces/{workspace_id}/layout", {
    params: { path: { workspace_id: id } }, body: layout, signal,
  }));
}

/** Get or create the caller's personal settings Artifact. */
export async function openPersonalSettings(): Promise<string> {
  const settings = await send(TIMEOUT_MS.write, undefined, (signal) => client.POST("/api/me/settings-artifact", { signal }));
  if (!settings || typeof settings.artifact_id !== "string") throw new Error("Invalid settings artifact");
  return settings.artifact_id;
}

/** Open the user's station that holds a chat, or make one bound to it. */
export async function stationForChat(conversationId: string): Promise<WorkspaceState> {
  return valid(await send(TIMEOUT_MS.write, undefined, (signal) => client.POST("/api/workspaces/for-chat/{conversation_id}", {
    params: { path: { conversation_id: conversationId } }, signal,
  })));
}

/** Hide or restore one of the user's stations in the bar. */
export async function hideStation(id: string, hidden = true): Promise<WorkspaceState> {
  return valid(await send(TIMEOUT_MS.write, undefined, (signal) => client.PATCH("/api/workspaces/{workspace_id}", {
    params: { path: { workspace_id: id } }, body: { hidden }, signal,
  })));
}

/** Pin or unpin one of the user's stations. */
export async function pinStation(id: string, pinned: boolean): Promise<WorkspaceState> {
  return valid(await send(TIMEOUT_MS.write, undefined, (signal) => client.PATCH("/api/workspaces/{workspace_id}", {
    params: { path: { workspace_id: id } }, body: { pinned }, signal,
  })));
}

/** Clear a station's chat and return the server-created station with its copied canvas. */
export async function clearChat(id: string, key: string): Promise<WorkspaceState> {
  return valid(await send(TIMEOUT_MS.write, undefined, (signal) => client.POST("/api/workspaces/{workspace_id}/clear-chat", {
    params: { path: { workspace_id: id }, header: { "Idempotency-Key": key } }, signal,
  })));
}

/** The Console agents a line was sent to directly, as the relay names them. */
export type RelayTarget = { readonly actor: string; readonly room: string | null };

/**
 * Tell a station's chat what the user said to agents from the Console: the chat
 * records one line for it and its partner hears it. False (the station has no chat,
 * its partner is down) is no failure. A retry under the same key tells it once.
 */
export async function relayToStation(id: string, targets: readonly RelayTarget[], text: string, key: string): Promise<boolean> {
  const told = await send(TIMEOUT_MS.write, undefined, (signal) => client.POST("/api/workspaces/{workspace_id}/relay", {
    params: { path: { workspace_id: id }, header: { "Idempotency-Key": key } },
    body: { targets: targets.map(({ actor, room }) => ({ actor, room })), text }, signal,
  }));
  return told.delivered;
}
