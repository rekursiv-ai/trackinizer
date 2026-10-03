import { afterEach, expect, test, vi } from "vitest";
import { getVisualCatalog } from "./visuals";
import { stubFetch } from "./testing";

afterEach(() => vi.unstubAllGlobals());

test("the catalog read rejects a malformed success instead of crashing the canvas", async () => {
  stubFetch(() => Response.json({ unrelated: true }));
  await expect(getVisualCatalog()).rejects.toThrow("Invalid visual catalog");
});

test("the catalog read returns backend visual descriptions", async () => {
  const catalog = { default_visual: "trax.browse", visuals: [{
    type: "trax.browse", version: 1, title: "Browse", description: "Browse trax records",
    requires: [], default_size: "wide", parameter_schema: {},
  }] };
  stubFetch(() => Response.json(catalog));
  await expect(getVisualCatalog()).resolves.toEqual(catalog);
});

test("development preview uses the checked catalog when the hosted route is older", async () => {
  stubFetch(() => Response.json({ detail: "Not Found" }, { status: 404 }));
  const catalog = await getVisualCatalog();
  expect(catalog.default_visual).toBe("trax.browse");
  expect(catalog.visuals.map((visual) => visual.type)).toEqual([
    "trax.browse", "trax.chat", "trax.subgraph", "trax.timeline", "trax.artifact",
  ]);
});
