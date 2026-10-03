import { afterEach, expect, test, vi } from "vitest";
import { readMetrics } from "./metrics";
import { stubFetch } from "./testing";

afterEach(() => {
  vi.unstubAllGlobals();
});

const ID = "00000000-0000-4000-8000-000000000001";

const point = (step: number) => ({ key: "loss", step, value: 1.5, kind: "scalar", timestamp: null });

test("metrics are one GET of the server's largest page, with no body", async () => {
  const points = [point(0)];
  const sent = stubFetch(() => Response.json({ points }));
  expect(await readMetrics(ID)).toEqual({ points, next: null });
  expect(sent).toEqual([
    { method: "GET", path: `/api/experiments/${ID}/metrics`, query: "?limit=1000", headers: {}, body: undefined },
  ]);
});

test("a full page asks for the one point after it, which says whether the run goes on", async () => {
  const run = Array.from({ length: 1001 }, (_, step) => point(step));
  const sent = stubFetch((request) => {
    const query = new URL(request.url).searchParams;
    const offset = Number(query.get("offset") ?? 0);
    return Response.json({ points: run.slice(offset, offset + Number(query.get("limit"))) });
  });
  expect(await readMetrics(ID)).toEqual({ points: run.slice(0, 1000), next: run[1000] });
  expect(sent.map((request) => request.query)).toEqual(["?limit=1000", "?limit=1&offset=1000"]);
  run.pop();
  expect((await readMetrics(ID)).next).toBeNull();
});

test("a step past 2^53 - 1 fails the read, since a number cannot hold it exactly", async () => {
  // The body as the server writes it: JSON.parse reads 9007199254740993 as ...992.
  const answer = (step: string) =>
    new Response(`{"points": [{"key": "loss", "step": ${step}, "value": 1.5, "kind": "scalar", "timestamp": null}]}`, {
      headers: { "content-type": "application/json" },
    });
  stubFetch(() => answer("9007199254740993"));
  await expect(readMetrics(ID)).rejects.toMatchObject({ status: 200, code: "unreadable" });
  stubFetch(() => answer("9007199254740991"));
  expect((await readMetrics(ID)).points[0]!.step).toBe(Number.MAX_SAFE_INTEGER);
});

test("a row that is not an Experiment fails with the server's message", async () => {
  stubFetch(() => Response.json({ detail: "not an Experiment", code: "conflict" }, { status: 409 }));
  await expect(readMetrics(ID)).rejects.toMatchObject({ status: 409, detail: "not an Experiment" });
});
