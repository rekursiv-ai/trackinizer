import { afterEach, expect, test, vi } from "vitest";
import { getEdgeTopology, getEnums, getFieldOwners } from "./meta";
import { stubFetch } from "./testing";

afterEach(() => {
  vi.unstubAllGlobals();
});

test("each meta call is a bodiless GET of its route", async () => {
  const bodies: { [path: string]: unknown } = {
    "/api/meta/enums": { inquiry_kind_all: ["Issue", "Belief"], status: ["active"] },
    "/api/meta/fields": { priority: "issue" },
    "/api/meta/edges": {
      narrows: { from_kinds: ["Issue"], to_kinds: ["Issue"], forward: "narrows", inverse: "narrowed_by" },
    },
  };
  const sent = stubFetch((request) => Response.json(bodies[new URL(request.url).pathname]));
  expect(await getEnums()).toEqual(bodies["/api/meta/enums"]);
  expect(await getFieldOwners()).toEqual(bodies["/api/meta/fields"]);
  expect(await getEdgeTopology()).toEqual(bodies["/api/meta/edges"]);
  expect(sent).toEqual(
    Object.keys(bodies).map((path) => ({ method: "GET", path, query: "", headers: {}, body: undefined })),
  );
});
