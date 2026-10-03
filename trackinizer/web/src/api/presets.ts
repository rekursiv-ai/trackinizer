import { type CallOptions, client, send, TIMEOUT_MS } from "./client";
import type { components } from "./generated/schema";

export type WorkspacePreset = components["schemas"]["WorkspacePreset"];
export type FloatingRect = components["schemas"]["FloatingRect"];

export type CreateWorkspacePreset = {
  readonly name: string;
  readonly agentInstructions: string | null;
  readonly continuationRecordId: string | null;
  readonly floatingRects: Readonly<Record<string, FloatingRect>>;
};

/** Read the signed-in account's saved canvas views and workflow snapshots. */
export async function listWorkspacePresets({ signal }: CallOptions = {}): Promise<WorkspacePreset[]> {
  const presets = await send(TIMEOUT_MS.read, signal, (signal) =>
    client.GET("/api/workspace-presets", { signal }));
  if (!Array.isArray(presets) || !presets.every(validPreset)) {
    throw new Error("Invalid workspace presets");
  }
  return presets;
}

/** Save one stable canvas revision with optional workflow guidance. */
export async function createWorkspacePreset(
  workspaceId: string,
  revision: number,
  preset: CreateWorkspacePreset,
  key: string,
): Promise<WorkspacePreset> {
  const created = await send(TIMEOUT_MS.write, undefined, (signal) =>
    client.POST("/api/workspace-presets", {
      params: { header: { "Idempotency-Key": key } },
      body: {
        workspace_id: workspaceId,
        revision,
        name: preset.name,
        agent_instructions: preset.agentInstructions,
        continuation_record_id: preset.continuationRecordId,
        floating_rects: preset.floatingRects,
      },
      signal,
    }));
  if (!validPreset(created)) throw new Error("Invalid workspace preset");
  return created;
}

/** Restore a saved canvas against its current revision; the server disconnects Chat. */
export async function openWorkspacePreset(
  presetId: string,
  workspaceId: string,
  revision: number,
  key: string,
): Promise<components["schemas"]["WorkspaceState"]> {
  const state = await send(TIMEOUT_MS.write, undefined, (signal) =>
    client.POST("/api/workspace-presets/{preset_id}/open", {
      params: { path: { preset_id: presetId }, header: { "Idempotency-Key": key } },
      body: { workspace_id: workspaceId, revision },
      signal,
    }));
  if (!validWorkspaceState(state)) throw new Error("Invalid restored workspace");
  return state;
}

function validPreset(value: unknown): value is WorkspacePreset {
  return typeof value === "object" && value !== null
    && "id" in value && typeof value.id === "string"
    && "name" in value && typeof value.name === "string"
    && "state" in value && typeof value.state === "object" && value.state !== null
    && "visuals" in value.state && Array.isArray(value.state.visuals);
}

function validWorkspaceState(value: unknown): value is components["schemas"]["WorkspaceState"] {
  return typeof value === "object" && value !== null
    && "id" in value && typeof value.id === "string"
    && "revision" in value && Number.isInteger(value.revision)
    && "visuals" in value && Array.isArray(value.visuals)
    && value.visuals.every((visual) => typeof visual === "object" && visual !== null
      && "id" in visual && typeof visual.id === "string"
      && "type" in visual && typeof visual.type === "string");
}
