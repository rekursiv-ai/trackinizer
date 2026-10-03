import { afterEach, expect, test, vi } from "vitest";
import { getArtifactContentRevision } from "./artifacts";
import { stubFetch } from "./testing";

afterEach(() => vi.unstubAllGlobals());

test("a canonical Artifact UUID matches an uppercase lookup", async () => {
  const lower = "0b6f7c1e-2f7a-4c55-9d7e-1f0e6b1d2a33";
  stubFetch(() => Response.json({
    revision: 1,
    artifact_id: lower,
    issue_id: lower,
    title: "Atlas",
    summary: "An Artifact.",
    author: "josh@example.com",
    created_at: "2026-09-29T10:00:00Z",
    scope: "team",
    format: "html",
    html: "<h1>Atlas</h1>",
    sections: [],
    citations: [],
  }));
  await expect(getArtifactContentRevision(lower.toUpperCase())).resolves.toMatchObject({
    title: "Atlas",
    scope: "team",
  });
});
