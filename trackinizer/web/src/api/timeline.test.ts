import { afterEach, expect, test, vi } from "vitest";
import { getEvidenceTimeline } from "./timeline";

afterEach(() => vi.unstubAllGlobals());

test("loads an evidence timeline with validated browser-selected bounds", async () => {
  const timeline = {
    target: { id: "record-id", kind: "Issue", seq: 21706, title: "Root", status: "active", created: "2026-09-29T00:00:00Z", modified: "2026-09-29T00:00:00Z", description: null, outcome: null },
    issue: { id: "record-id", kind: "Issue", seq: 21706, title: "Root", status: "active", created: "2026-09-29T00:00:00Z", modified: "2026-09-29T00:00:00Z", description: null, outcome: null },
    selected_result: null, root_results: [], root_results_truncated: false,
    directions: [], directions_truncated: false,
    unresolved_questions: [],
  };
  vi.stubGlobal("fetch", async (request: Request) => {
    const url = new URL(request.url);
    expect(url.pathname).toBe("/api/visuals/timeline/record-id");
    expect(url.searchParams.get("direction_limit")).toBe("6");
    expect(url.searchParams.get("results_per_direction")).toBe("2");
    return Response.json(timeline);
  });
  await expect(getEvidenceTimeline("record-id", {
    directionLimit: 6, resultsPerDirection: 2,
  })).resolves.toEqual(timeline);
});

test("rejects a malformed timeline instead of rendering unbounded data", async () => {
  vi.stubGlobal("fetch", async () => Response.json({ directions: "all" }));
  await expect(getEvidenceTimeline("record-id")).rejects.toThrow("Invalid evidence timeline");
});
