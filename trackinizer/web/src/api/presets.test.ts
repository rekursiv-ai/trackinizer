import { afterEach, expect, test, vi } from "vitest";
import { createWorkspacePreset, listWorkspacePresets, openWorkspacePreset } from "./presets";

afterEach(() => vi.unstubAllGlobals());

test("lists named workspace presets through the typed API client", async () => {
  const preset = {
    id: "preset-id", name: "Triage", agent_instructions: null, continuation_record_id: null,
    state: { visuals: [], focused_instance: null, agent_instructions: null, continuation_record_id: null },
    created_at: "2026-09-29T08:00:00Z", modified_at: "2026-09-29T08:00:00Z",
  };
  vi.stubGlobal("fetch", async (request: Request) => {
    expect(new URL(request.url).pathname).toBe("/api/workspace-presets");
    expect(request.method).toBe("GET");
    return Response.json([preset]);
  });
  await expect(listWorkspacePresets()).resolves.toEqual([preset]);
});

test("creates a named workflow with its current snapshot and continuation details", async () => {
  const preset = {
    id: "preset-id", name: "Triage", agent_instructions: "Summarize",
    continuation_record_id: "record-id",
    state: { visuals: [], focused_instance: null, agent_instructions: "Summarize", continuation_record_id: "record-id" },
    created_at: "2026-09-29T08:00:00Z", modified_at: "2026-09-29T08:00:00Z",
  };
  vi.stubGlobal("fetch", async (request: Request) => {
    expect(request.method).toBe("POST");
    expect(request.headers.get("Idempotency-Key")).toBe("save-key");
    expect(await request.json()).toEqual({
      workspace_id: "workspace-id", revision: 7, name: "Triage", agent_instructions: "Summarize",
      continuation_record_id: "record-id", floating_rects: {},
    });
    return Response.json(preset);
  });
  await expect(createWorkspacePreset("workspace-id", 7, {
    name: "Triage", agentInstructions: "Summarize", continuationRecordId: "record-id", floatingRects: {},
  }, "save-key")).resolves.toEqual(preset);
});

test("opens a saved view against the current revision and returns the disconnected canvas", async () => {
  const state = { id: "workspace-id", revision: 8, visuals: [], focused_instance: null };
  vi.stubGlobal("fetch", async (request: Request) => {
    expect(new URL(request.url).pathname).toBe("/api/workspace-presets/preset-id/open");
    expect(request.method).toBe("POST");
    expect(request.headers.get("Idempotency-Key")).toBe("open-key");
    expect(await request.json()).toEqual({ workspace_id: "workspace-id", revision: 7 });
    return Response.json(state);
  });
  await expect(openWorkspacePreset("preset-id", "workspace-id", 7, "open-key")).resolves.toEqual(state);
});

test("a failed save preserves the server error for the canvas to display", async () => {
  vi.stubGlobal("fetch", async () => Response.json({ detail: "An account can save at most 100 presets." }, { status: 422 }));
  await expect(createWorkspacePreset("workspace-id", 7, {
    name: "Triage", agentInstructions: null, continuationRecordId: null, floatingRects: {},
  }, "save-key")).rejects.toMatchObject({ status: 422, detail: "An account can save at most 100 presets." });
});
