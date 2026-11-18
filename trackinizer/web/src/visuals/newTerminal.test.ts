import { QueryClient } from "@tanstack/react-query";
import { afterEach, expect, test, vi } from "vitest";
import { applyWorkspaceOperation, type WorkspaceState } from "../api/workspaces";
import { showNewTerminal, takeNewTerminalRequest } from "./newTerminal";

vi.mock("../api/workspaces", () => ({ applyWorkspaceOperation: vi.fn() }));

const station = (visuals: WorkspaceState["visuals"]) => ({ id: "w1", revision: 4, visuals }) as WorkspaceState;
const tile = { id: "v1", type: "trax.terminal", version: 1, placement: "main", record_id: null, params: { machine: "gx10" } } as WorkspaceState["visuals"][number];

afterEach(() => { takeNewTerminalRequest(); vi.clearAllMocks(); });

test("with no tile on the canvas it shows one, naming no params, and leaves a request for it", async () => {
  vi.mocked(applyWorkspaceOperation).mockResolvedValue(station([tile]));
  await showNewTerminal(new QueryClient(), station([]));
  expect(applyWorkspaceOperation).toHaveBeenCalledWith("w1", 4, { kind: "show", visual_type: "trax.terminal", placement: "main" }, expect.any(String));
  expect(takeNewTerminalRequest()).toBe(true);
  expect(takeNewTerminalRequest()).toBe(false);
});

test("with a tile on the canvas it focuses that one and leaves a request", async () => {
  vi.mocked(applyWorkspaceOperation).mockResolvedValue(station([tile]));
  await showNewTerminal(new QueryClient(), station([tile]));
  expect(applyWorkspaceOperation).toHaveBeenCalledWith("w1", 4, { kind: "focus", instance_id: "v1" }, expect.any(String));
  expect(takeNewTerminalRequest()).toBe(true);
});

test("a write that fails takes the request back, so a later tile does not open a shell nobody asked for", async () => {
  vi.mocked(applyWorkspaceOperation).mockRejectedValue(new Error("conflict"));
  await expect(showNewTerminal(new QueryClient(), station([]))).rejects.toThrow("conflict");
  expect(takeNewTerminalRequest()).toBe(false);
});
