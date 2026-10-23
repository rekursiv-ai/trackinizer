import { QueryClient } from "@tanstack/react-query";
import { expect, test } from "vitest";
import type { CanvasState } from "../api/canvases";
import { acceptCanvas, newerCanvas } from "./cache";

const canvas: CanvasState = { id: "w1", name: "unnamed-canvas#1", conversation_id: null, page: null, revision: 3, focused_instance: null, visuals: [] };

test("newerCanvas keeps the newer revision, a tie going to the incoming one", () => {
  const older = { ...canvas, revision: 2 };
  expect(newerCanvas(canvas, older)).toBe(canvas);
  expect(newerCanvas(older, canvas)).toBe(canvas);
  expect(newerCanvas(undefined, older)).toBe(older);
  const tie = { ...canvas };
  expect(newerCanvas(canvas, tie)).toBe(tie);
});

test("acceptCanvas fills its canvas's key and never replaces a newer state", () => {
  const client = new QueryClient();
  acceptCanvas(client, canvas);
  expect(client.getQueryData(["canvas", "w1"])).toBe(canvas);
  acceptCanvas(client, { ...canvas, revision: 1 });
  expect(client.getQueryData(["canvas", "w1"])).toBe(canvas);
});
