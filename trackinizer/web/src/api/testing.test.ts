import { setFlagsFromString } from "node:v8";
import { runInNewContext } from "node:vm";
import { afterEach, expect, test, vi } from "vitest";
import { stubFetch } from "./testing";

afterEach(() => {
  vi.unstubAllGlobals();
});

test("an answer that waits for the request's abort gets it, though a garbage collection ran meanwhile", async () => {
  setFlagsFromString("--expose-gc");
  const gc = runInNewContext("gc") as () => void;
  stubFetch((request) => new Promise((_, reject) => request.signal.addEventListener("abort", () => reject(request.signal.reason))));
  const controller = new AbortController();
  // Nothing here keeps the request: only the fake fetch can.
  const answer = fetch(new Request("http://localhost/slow", { signal: controller.signal })).catch((error: unknown) => error);
  await new Promise((resolve) => setTimeout(resolve));
  gc();
  controller.abort();
  expect(await answer).toMatchObject({ name: "AbortError" });
});
