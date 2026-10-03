import { afterEach, expect, test, vi } from "vitest";
import { readHistogram } from "./histogram";
import { stubFetch } from "./testing";

afterEach(() => {
  vi.unstubAllGlobals();
});

test("the histogram is a GET with the feed's filters repeated, its window and its bucket count; unset ones are left out", async () => {
  const answer = {
    start: "2026-10-02T20:00:00+00:00",
    end: "2026-10-02T21:00:00+00:00",
    bucket_seconds: 1800,
    counts: [{ start: "2026-10-02T20:00:00+00:00", count: 3 }],
  };
  const sent = stubFetch(() => Response.json(answer));
  expect(
    await readHistogram({
      actor: ["codex-a", "codex-b"],
      room: ["ops"],
      cli: ["codex"],
      kind: ["UserMessage", "AssistantMessage"],
      since: "2026-10-02T20:00:00.000Z",
      until: "2026-10-02T21:00:00.000Z",
      buckets: 2,
    }),
  ).toEqual(answer);
  await readHistogram({ buckets: 1000 });
  // An empty filter filters nothing, as an unset one.
  await readHistogram({ actor: [], room: [], cli: [], kind: [] });
  expect(sent.map((request) => [request.method, request.path, [...new URLSearchParams(request.query)]])).toEqual([
    [
      "GET",
      "/api/web/feed/histogram",
      [
        ["actor", "codex-a"],
        ["actor", "codex-b"],
        ["room", "ops"],
        ["cli", "codex"],
        ["kind", "UserMessage"],
        ["kind", "AssistantMessage"],
        ["since", "2026-10-02T20:00:00.000Z"],
        ["until", "2026-10-02T21:00:00.000Z"],
        ["buckets", "2"],
      ],
    ],
    ["GET", "/api/web/feed/histogram", [["buckets", "1000"]]],
    ["GET", "/api/web/feed/histogram", []],
  ]);
});
