import { afterEach, expect, test, vi } from "vitest";
import focusFixture from "../../test/testdata/graph/getGraph.focus.json";
import fixture from "../../test/testdata/graph/getGraph.json";
import { ALL_NODES, type FocusGraph, type Graph, getGraph, getGraphFocus } from "./graph";
import { stubFetch } from "./testing";

afterEach(() => {
  vi.unstubAllGlobals();
});

test("getGraph is a bodiless GET of /api/web/graph with the limit, as the fixture recorded it", async () => {
  // The server's recorded answer has the row types written in ./graph, so tsc checks them.
  const body: Graph = fixture.response.body;
  const sent = stubFetch(() => Response.json(body));
  expect(await getGraph(1000)).toEqual(body);
  expect(sent).toEqual([
    {
      method: fixture.request.method,
      path: fixture.request.path,
      query: `?${new URLSearchParams(fixture.request.query)}`,
      headers: {},
      body: undefined,
    },
  ]);
});

test("ALL_NODES asks for every inquiry: the largest limit an SQL integer holds", async () => {
  const sent = stubFetch(() => Response.json({ nodes: [], edges: [] }));
  await getGraph(ALL_NODES);
  expect(sent.map(({ query }) => query)).toEqual(["?limit=2147483647"]);
});

test("getGraphFocus is a bodiless GET of /api/web/graph with the focus, hops and limit, as the fixture recorded it", async () => {
  // As for getGraph, tsc checks the recorded answer against FocusGraph: every node has its hops.
  const body: FocusGraph = focusFixture.response.body;
  const sent = stubFetch(() => Response.json(body));
  const focus = focusFixture.request.query[0]![1]!;
  expect(await getGraphFocus({ focus, hops: 2, limit: 60 })).toEqual(body);
  expect(sent).toEqual([
    {
      method: focusFixture.request.method,
      path: focusFixture.request.path,
      query: `?${new URLSearchParams(focusFixture.request.query)}`,
      headers: {},
      body: undefined,
    },
  ]);
});

test("getGraphFocus leaves hops and limit to the server when unset", async () => {
  const sent = stubFetch(() => Response.json({ nodes: [], edges: [] }));
  await getGraphFocus({ focus: "00000000-0000-4000-8000-000000000001" });
  expect(sent.map(({ query }) => query)).toEqual(["?focus=00000000-0000-4000-8000-000000000001"]);
});
