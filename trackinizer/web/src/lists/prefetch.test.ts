import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { type Sent, stubFetch } from "../api/testing";
import { detailQueries } from "../detail/queries";
import { testClient, uuid } from "../live/testing";
import { DetailWarmer } from "./prefetch";

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

/** A warmer over a fresh cache, and the detail reads it makes, by id. */
function warmer() {
  const sent = stubFetch((request) => {
    const id = new URL(request.url).pathname.split("/").at(-1)!;
    return Response.json({ self: { id, kind: "Issue", seq: 1, title: "", status: "active" }, edges: {}, backlinks: {}, changes: [] });
  });
  const client = testClient();
  return { client, warm: new DetailWarmer(client), reads: () => reads(sent) };
}

function reads(sent: readonly Sent[]): string[] {
  return sent.filter((request) => request.path.startsWith("/api/web/get/")).map((request) => request.path.split("/").at(-1)!);
}

test("a row rested on for 150 ms has its detail read, into the detail's own query", async () => {
  const { client, warm, reads } = warmer();
  warm.intend(uuid(1));
  await vi.advanceTimersByTimeAsync(149);
  expect(reads()).toEqual([]);
  await vi.advanceTimersByTimeAsync(1);
  expect(reads()).toEqual([uuid(1)]);
  expect(client.getQueryData(detailQueries.detail(uuid(1)).queryKey)).toMatchObject({ self: { id: uuid(1) } });
});

test("a row passed over sooner, or left, is not read", async () => {
  const { warm, reads } = warmer();
  warm.intend(uuid(1));
  await vi.advanceTimersByTimeAsync(100);
  warm.intend(uuid(2));
  await vi.advanceTimersByTimeAsync(100);
  warm.intend(null);
  await vi.advanceTimersByTimeAsync(1_000);
  expect(reads()).toEqual([]);
});

test("a detail read in the last 30 s is not read again", async () => {
  const { warm, reads } = warmer();
  warm.intend(uuid(1));
  await vi.advanceTimersByTimeAsync(150);
  warm.intend(null);
  warm.intend(uuid(1));
  await vi.advanceTimersByTimeAsync(150);
  expect(reads()).toEqual([uuid(1)]);
  await vi.advanceTimersByTimeAsync(30_000);
  warm.intend(null);
  warm.intend(uuid(1));
  await vi.advanceTimersByTimeAsync(150);
  expect(reads()).toEqual([uuid(1), uuid(1)]);
});

test("at most 10 rows a minute are read ahead", async () => {
  const { warm, reads } = warmer();
  for (let n = 1; n <= 12; n++) {
    warm.intend(uuid(n));
    await vi.advanceTimersByTimeAsync(150);
  }
  expect(reads()).toHaveLength(10);
  await vi.advanceTimersByTimeAsync(60_000);
  warm.intend(uuid(13));
  await vi.advanceTimersByTimeAsync(150);
  expect(reads().at(-1)).toBe(uuid(13));
});
