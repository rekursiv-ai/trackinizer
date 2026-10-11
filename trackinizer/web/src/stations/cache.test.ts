import { QueryClient } from "@tanstack/react-query";
import { expect, test } from "vitest";
import type { WorkspaceState } from "../api/workspaces";
import { acceptWorkspace, newerWorkspace } from "./cache";

const workspace: WorkspaceState = { id: "w1", name: "unnamed-workstream#1", conversation_id: null, page: null, revision: 3, focused_instance: null, visuals: [] };

test("newerWorkspace keeps the newer revision, a tie going to the incoming one", () => {
  const older = { ...workspace, revision: 2 };
  expect(newerWorkspace(workspace, older)).toBe(workspace);
  expect(newerWorkspace(older, workspace)).toBe(workspace);
  expect(newerWorkspace(undefined, older)).toBe(older);
  const tie = { ...workspace };
  expect(newerWorkspace(workspace, tie)).toBe(tie);
});

test("acceptWorkspace fills its station's key and never replaces a newer state", () => {
  const client = new QueryClient();
  acceptWorkspace(client, workspace);
  expect(client.getQueryData(["workspace", "w1"])).toBe(workspace);
  acceptWorkspace(client, { ...workspace, revision: 1 });
  expect(client.getQueryData(["workspace", "w1"])).toBe(workspace);
});
