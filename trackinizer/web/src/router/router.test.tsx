import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test } from "vitest";
import { watchEvents } from "../debug/testing";
import { formatRoute } from "./route";
import { RouterProvider, useRouter } from "./router";

const KINDS = ["Issue", "Belief"];

beforeEach(() => {
  history.replaceState(null, "", "#/activity");
});

afterEach(cleanup);

/** Shows the route the app would render, as the hash it formats to. */
function Shown() {
  return <output>{formatRoute(useRouter().route)}</output>;
}

function renderRouter(): { navigate: ReturnType<typeof useRouter>["navigate"] } {
  const handle = {} as { navigate: ReturnType<typeof useRouter>["navigate"] };
  function Grab() {
    handle.navigate = useRouter().navigate;
    return null;
  }
  render(
    <RouterProvider kinds={KINDS}>
      <Shown />
      <Grab />
    </RouterProvider>,
  );
  return handle;
}

/** Traverse history and wait for the router to hear about it. */
async function traverse(step: () => void): Promise<void> {
  await act(async () => {
    const heard = new Promise((resolve) => addEventListener("hashchange", resolve, { once: true }));
    step();
    await heard;
  });
}

test("Back then Forward leaves the URL and the view in agreement (COLD-03)", async () => {
  const { navigate } = renderRouter();
  act(() => navigate({ name: "list", kind: "Belief" }));
  await act(async () => {});
  act(() => navigate({ name: "ref", kind: "Issue", seq: 7 }));
  await act(async () => {});
  expect(screen.getByRole("status").textContent).toBe("#/ref/Issue/7");

  await traverse(() => history.back());
  expect(location.hash).toBe("#/list/Belief");
  expect(screen.getByRole("status").textContent).toBe("#/list/Belief");

  await traverse(() => history.forward());
  expect(location.hash).toBe("#/ref/Issue/7");
  expect(screen.getByRole("status").textContent).toBe("#/ref/Issue/7");
});

test("an old UI link is replaced by its v2 form without a history entry", async () => {
  const before = history.length;
  history.replaceState(null, "", "#/recent");
  renderRouter();
  expect(location.hash).toBe("#/activity");
  expect(screen.getByRole("status").textContent).toBe("#/activity");
  expect(history.length).toBe(before);
});

test("replace swaps the entry; a plain navigate adds one", async () => {
  const { navigate } = renderRouter();
  const before = history.length;
  act(() => navigate({ name: "list", kind: "Issue" }, { replace: true }));
  expect(screen.getByRole("status").textContent).toBe("#/list/Issue");
  expect(history.length).toBe(before);
  act(() => navigate({ name: "list", kind: "Belief" }));
  await act(async () => {});
  expect(screen.getByRole("status").textContent).toBe("#/list/Belief");
  expect(history.length).toBe(before + 1);
});

test("each route shown is logged by name, kind, seq and id, never search text, and a non-canonical hash once", async () => {
  const logged = watchEvents();
  const shown = () => logged().filter(({ event }) => event === "navigate").map(({ fields }) => fields);
  history.replaceState(null, "", "#/recent");
  const { navigate } = renderRouter();
  act(() => navigate({ name: "ref", kind: "Issue", seq: 7 }));
  await act(async () => {});
  act(() => navigate({ name: "search", q: "my private words" }));
  await act(async () => {});
  expect(shown()).toEqual([
    { route: "activity" },
    { route: "ref", kind: "Issue", seq: 7 },
    { route: "search" },
  ]);
});
