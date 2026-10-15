import { type CallOptions, client, send, TIMEOUT_MS } from "./client";
import type { components } from "./generated/schema";
import { valid, type WorkspaceState } from "./workspaces";

/** One station in the user's list: its name, its chat's conversation and the page it last showed. */
export type StationSummary = components["schemas"]["WorkspaceSummary"];

/** A station's default campaign settings; any subset, bounded and defaulted at launch. */
export type CampaignSetup = components["schemas"]["CampaignSetup"];

/** The user's stations, the one changed last first. */
export async function listStations({ signal }: CallOptions = {}): Promise<StationSummary[]> {
  const stations = await send(TIMEOUT_MS.read, signal, (signal) => client.GET("/api/workspaces", { signal }));
  if (!Array.isArray(stations) || !stations.every((each) => typeof each?.id === "string" && typeof each.name === "string")) {
    throw new Error("Invalid station list");
  }
  return stations;
}

/** Make a station, named `unnamed-workstream#N`, and open it. */
export async function createStation({ signal }: CallOptions = {}): Promise<WorkspaceState> {
  return valid(await send(TIMEOUT_MS.write, signal, (signal) => client.POST("/api/workspaces/new", { signal })));
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

/**
 * Store a station's default campaign setup, or clear it with null. The whole setup
 * replaces what was stored; a launch from the station's chat takes it where the
 * asker's line leaves a field unset. The server validates the shape and returns the
 * station with its setup as stored.
 */
export async function setStationCampaign(id: string, campaign: CampaignSetup | null): Promise<WorkspaceState> {
  return valid(await send(TIMEOUT_MS.write, undefined, (signal) => client.PUT("/api/workspaces/{workspace_id}/campaign", {
    params: { path: { workspace_id: id } }, body: { campaign }, signal,
  })));
}

/** Open the user's station that holds a chat, or make one bound to it. */
export async function stationForChat(conversationId: string): Promise<WorkspaceState> {
  return valid(await send(TIMEOUT_MS.write, undefined, (signal) => client.POST("/api/workspaces/for-chat/{conversation_id}", {
    params: { path: { conversation_id: conversationId } }, signal,
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
