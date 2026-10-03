import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { ApiError } from "../api/client";
import { errorDetails, pageRoute } from "./details";
import { log } from "./log";

const SENT = {
  method: "PUT",
  path: "/api/inquiries/0b6f7c1e-2f7a-4c55-9d7e-1f0e6b1d2a33/title",
  id: "5d3c2b1a-0f9e-4d8c-8b7a-6e5d4c3b2a19",
  at: "2026-09-27T10:00:00.000Z",
  ms: 84,
  attempt: 2,
};

beforeEach(() => {
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
  history.replaceState(null, "", location.pathname);
});

test("Copy details has the message, time, page, build and browser, the failed request, and the recent events", () => {
  history.replaceState(null, "", "#/ref/Issue/412");
  log("warn", "earlier", { n: 1 });
  const text = errorDetails("Not saved: the server failed (database unavailable).", new ApiError(503, "database unavailable", null, SENT));
  const lines = text.split("\n");
  expect(lines.slice(0, 3)).toEqual([
    "Trackinizer web app: Not saved: the server failed (database unavailable).",
    expect.stringMatching(/^time: \d{4}-\d\d-\d\dT/),
    "page: #/ref/Issue/412",
  ]);
  expect(lines[3]).toBe(`build: ${__COMMIT__}`);
  expect(__COMMIT__).toMatch(/^[0-9a-f]{40}$/);
  expect(lines[4]).toBe(`browser: ${navigator.userAgent}`);
  expect(lines[5]).toBe(
    `failed: request method=PUT path=${SENT.path} status=503 ms=84 request_id=${SENT.id} attempt=2 at=${SENT.at}`,
  );
  expect(lines[6]).toBe("recent events:");
  expect(lines.at(-1)).toMatch(/^\d{4}-.*Z warn earlier n=1$/);
});

test("a failure without a request says what it can; any other error keeps its stack, and none adds nothing", () => {
  const lines = (error: unknown) => {
    const all = errorDetails("m", error).split("\n");
    return all.slice(5, all.indexOf("recent events:"));
  };
  expect(lines(new ApiError(0, "No response within 30 s.", "timeout"))).toEqual(["failed: request status=0 code=timeout"]);
  expect(lines(null)).toEqual([]);
  const crash = new TypeError("kinds.find is not a function");
  const text = errorDetails("The page crashed", crash);
  expect(text).toContain("\nerror: TypeError: kinds.find is not a function\nstack: TypeError: kinds.find is not a function\n    at ");
});

test("the page's route leaves out search text and a hash's query", () => {
  const route = (hash: string) => {
    history.replaceState(null, "", hash || location.pathname);
    return pageRoute();
  };
  expect(route("#/search/my%20private%20words")).toBe("#/search/…");
  expect(route("#/search?q=private")).toBe("#/search");
  expect(route("#/list/Issue")).toBe("#/list/Issue");
  expect(route("")).toBe("#/");
});
