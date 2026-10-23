import { QueryClient } from "@tanstack/react-query";
import { afterEach, expect, test, vi } from "vitest";
import { applyCanvasOperation, type CanvasState } from "../api/canvases";
import { showNewTerminal, takeNewTerminalRequest } from "./newTerminal";

vi.mock("../api/canvases", () => ({ applyCanvasOperation: vi.fn() }));

const canvas = (visuals: CanvasState["visuals"]) => ({ id: "w1", revision: 4, visuals }) as CanvasState;
const tile = { id: "v1", type: "trax.terminal", version: 1, placement: "main", record_id: null, params: { machine: "gx10" } } as CanvasState["visuals"][number];

afterEach(() => { takeNewTerminalRequest(); vi.clearAllMocks(); });

test("with no tile on the canvas it shows one, naming no params, and leaves a request for it", async () => {
  vi.mocked(applyCanvasOperation).mockResolvedValue(canvas([tile]));
  await showNewTerminal(new QueryClient(), canvas([]));
  expect(applyCanvasOperation).toHaveBeenCalledWith("w1", 4, { kind: "show", visual_type: "trax.terminal", placement: "main" }, expect.any(String));
  expect(takeNewTerminalRequest()).toBe(true);
  expect(takeNewTerminalRequest()).toBe(false);
});

test("with a tile on the canvas it focuses that one and leaves a request", async () => {
  vi.mocked(applyCanvasOperation).mockResolvedValue(canvas([tile]));
  await showNewTerminal(new QueryClient(), canvas([tile]));
  expect(applyCanvasOperation).toHaveBeenCalledWith("w1", 4, { kind: "focus", instance_id: "v1" }, expect.any(String));
  expect(takeNewTerminalRequest()).toBe(true);
});

test("a write that fails takes the request back, so a later tile does not open a shell nobody asked for", async () => {
  vi.mocked(applyCanvasOperation).mockRejectedValue(new Error("conflict"));
  await expect(showNewTerminal(new QueryClient(), canvas([]))).rejects.toThrow("conflict");
  expect(takeNewTerminalRequest()).toBe(false);
});
