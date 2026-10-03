import { cleanup, render, screen } from "@testing-library/react";
import { Suspense } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { ActivityView, GraphView, preloadView } from "./views";

vi.mock("../activity", () => ({ ActivityView: () => <h1>Activity</h1> }));
vi.mock("../graph", () => ({ GraphView: () => <h1>Graph</h1> }));
vi.mock("../settings", () => {
  throw new Error("The settings chunk is gone.");
});

afterEach(cleanup);

test("preloadView settles once the view's chunk has loaded, so its first render does not suspend", async () => {
  await preloadView("#/activity");
  render(
    <Suspense fallback={<p>Loading</p>}>
      <ActivityView />
    </Suspense>,
  );
  expect(screen.getByRole("heading").textContent).toBe("Activity");
});

test("preloadView settles when the chunk fails too: the view reports it when it renders", async () => {
  await expect(preloadView("#/settings")).resolves.toBeUndefined();
});

test("preloadView loads the graph for a focus link, whose kind is in the hash's query", async () => {
  await preloadView("#/graph?focus=Issue/7&hops=2");
  render(
    <Suspense fallback={<p>Loading</p>}>
      <GraphView />
    </Suspense>,
  );
  expect(screen.getByRole("heading").textContent).toBe("Graph");
});
