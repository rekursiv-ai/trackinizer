import { afterEach, expect, test, vi } from "vitest";
import { retryRead } from "../app/queryClient";
import { recentEvents } from "../debug/log";
import { ApiError, client, send, TIMEOUT_MS } from "./client";
import { keyed } from "./idempotency";
import { setField } from "./inquiries";
import { getProfile } from "./me";
import { searchInquiries } from "./search";

const ID = "0b6f7c1e-2f7a-4c55-9d7e-1f0e6b1d2a33";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function profile(signal?: AbortSignal): Promise<unknown> {
  return send(TIMEOUT_MS.read, signal, (signal) => client.GET("/api/me/profile", { signal }));
}

/** A `fetch` that never answers, and rejects as `fetch` does once aborted. */
function hang(request: Request): Promise<Response> {
  return new Promise((_, reject) => {
    // Already aborted: `fetch` rejects at once, and no abort event would come.
    if (request.signal.aborted) reject(request.signal.reason);
    request.signal.addEventListener("abort", () => reject(request.signal.reason));
  });
}

/** Stub `fetch` with `answer`; returns the requests, headers and all. */
function stubRaw(answer: (request: Request) => Response | Promise<Response>): Request[] {
  const sent: Request[] = [];
  vi.stubGlobal("fetch", (request: Request) => {
    sent.push(request);
    return answer(request);
  });
  return sent;
}

/** What `promise` rejects with. */
function failure(promise: Promise<unknown>): Promise<ApiError> {
  return promise.then(
    () => {
      throw new Error("Expected a failure.");
    },
    (error: unknown) => {
      expect(error).toBeInstanceOf(ApiError);
      return error as ApiError;
    },
  );
}

test("a domain error carries the server's detail and code", async () => {
  vi.stubGlobal("fetch", async () =>
    Response.json({ detail: "status is complete, expected active", code: "cas" }, { status: 409 }),
  );
  expect(await failure(profile())).toMatchObject({ status: 409, detail: "status is complete, expected active", code: "cas" });
});

test("a 422 lists one line per field", async () => {
  const detail = [
    { loc: ["body", "value"], msg: "Input should be a valid string", type: "string_type" },
    { loc: ["query", "limit"], msg: "Input should be greater than 0", type: "greater_than" },
    { loc: [], msg: "Unknown field", type: "extra" },
  ];
  vi.stubGlobal("fetch", async () => Response.json({ detail }, { status: 422 }));
  expect((await failure(profile())).detail).toBe(
    "value: Input should be a valid string\n" +
      "query.limit: Input should be greater than 0\n" +
      "Unknown field",
  );
});

test("a body that is not the server's falls back to the status line", async () => {
  vi.stubGlobal("fetch", async () =>
    new Response("<html>bad gateway</html>", { status: 502, statusText: "Bad Gateway" }),
  );
  expect(await failure(profile())).toMatchObject({ status: 502, detail: "502 Bad Gateway", code: null });
});

test("reads time out after 15 s and writes after 30 s, as status 0", async () => {
  vi.useFakeTimers();
  const sent = stubRaw(hang);
  const read = failure(getProfile());
  const write = failure(setField("/api/inquiries/{target_id}/title", ID, keyed({ value: "x" })));
  await vi.advanceTimersByTimeAsync(TIMEOUT_MS.read - 1);
  expect(sent.map((request) => request.signal.aborted)).toEqual([false, false]);
  await vi.advanceTimersByTimeAsync(1);
  expect(await read).toMatchObject({ status: 0, detail: "No response within 15 s.", code: "timeout" });
  await vi.advanceTimersByTimeAsync(TIMEOUT_MS.write - TIMEOUT_MS.read - 1);
  expect(sent[1]!.signal.aborted).toBe(false);
  await vi.advanceTimersByTimeAsync(1);
  expect(await write).toMatchObject({ status: 0, detail: "No response within 30 s.", code: "timeout" });
});

test("a success whose body is not JSON is the server's answer, not a lost connection (WEB-03)", async () => {
  // A proxy's or a login page's HTML under a 200: it came, so it is neither a
  // network failure nor worth retrying, and a write it answered went through.
  const sent = stubRaw(async () =>
    new Response("<!doctype html><title>Sign in</title>", { headers: { "content-type": "text/html" } }),
  );
  const error = await failure(profile());
  expect(error).toMatchObject({ status: 200, detail: "The server answered 200 with text/html, not JSON.", code: "unreadable" });
  expect(error.sent?.id).toBe(sent[0]!.headers.get("X-Request-ID"));
  expect(retryRead(0, error)).toBe(false);
});

// openapi-fetch parses with `response.json()` when the length is known, and with
// `JSON.parse` of `response.text()` otherwise; neither parse may read as a lost
// connection, which would be retried and reported as the network's fault.
test.each([
  ["with a length", { "content-length": "9" }],
  ["without one", {}],
])("a success whose JSON does not parse is the server's answer under its status, %s", async (_, length) => {
  const sent = stubRaw(async () =>
    new Response("{not json", { status: 201, headers: { "content-type": "application/json", ...length } }),
  );
  const error = await failure(profile());
  expect(error).toMatchObject({ status: 201, detail: "The server answered 201 with JSON it could not read.", code: "unreadable" });
  expect(error.sent?.id).toBe(sent[0]!.headers.get("X-Request-ID"));
  expect(retryRead(0, error)).toBe(false);
});

test("a network failure is status 0", async () => {
  vi.stubGlobal("fetch", async () => {
    throw new TypeError("Failed to fetch");
  });
  expect(await failure(profile())).toMatchObject({
    status: 0,
    detail: "The server could not be reached: Failed to fetch",
    code: "network",
  });
});

test("a caller's abort rejects with the caller's reason, not an ApiError", async () => {
  vi.stubGlobal("fetch", hang);
  const controller = new AbortController();
  const aborted = profile(controller.signal).catch((error: unknown) => error);
  controller.abort();
  const error = await aborted;
  expect(error).not.toBeInstanceOf(ApiError);
  expect((error as Error).name).toBe("AbortError");
});

test("a page over plain HTTP, which lacks crypto.randomUUID, still sends each request's id (B1)", async () => {
  const real = globalThis.crypto;
  vi.stubGlobal("crypto", { getRandomValues: real.getRandomValues.bind(real) });
  const sent = stubRaw(() => Response.json({}));
  await profile();
  expect(sent[0]!.headers.get("X-Request-ID")).toMatch(UUID);
});

test("every request carries its own X-Request-ID, a UUID", async () => {
  const sent = stubRaw(() => Response.json({}));
  await profile();
  await profile();
  const [first, second] = sent.map((request) => request.headers.get("X-Request-ID"));
  expect(first).toMatch(UUID);
  expect(second).toMatch(UUID);
  expect(second).not.toBe(first);
});

test("a failure carries the request it came from, and logs one warning with its id and never the server's text", async () => {
  const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
  const sent = stubRaw(() => Response.json({ detail: "database unavailable" }, { status: 503 }));
  const error = await failure(setField("/api/inquiries/{target_id}/title", ID, keyed({ value: "My secret title" })));
  const id = sent[0]!.headers.get("X-Request-ID")!;
  expect(error.sent).toEqual({
    method: "PUT",
    path: `/api/inquiries/${ID}/title`,
    id,
    at: expect.stringMatching(/^\d{4}-\d\d-\d\dT/),
    ms: expect.any(Number),
    attempt: 1,
  });
  expect(warn).toHaveBeenCalledTimes(1);
  const line = String(warn.mock.calls[0]![0]);
  expect(line).toMatch(
    new RegExp(`^trackinizer request.failed method=PUT path=/api/inquiries/${ID}/title status=503 ms=\\d+ request_id=${id} attempt=1$`),
  );
  expect(recentEvents().at(-1)).toMatchObject({ level: "warn", event: "request.failed", fields: { request_id: id } });
});

test("no answer carries the request too, with the browser's reason", async () => {
  const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
  const sent = stubRaw(async () => {
    throw new TypeError("Failed to fetch");
  });
  const error = await failure(profile());
  expect(error.sent).toMatchObject({ method: "GET", path: "/api/me/profile", id: sent[0]!.headers.get("X-Request-ID") });
  expect(String(warn.mock.calls[0]![0])).toContain('code=network ms=');
  expect(String(warn.mock.calls[0]![0])).toContain('detail="The server could not be reached: Failed to fetch"');
});

test("the attempt counts the same request failing in a row: method, URL and key; a success starts it over", async () => {
  vi.spyOn(console, "warn").mockImplementation(() => {});
  let answer = () => Response.json({ detail: "down" }, { status: 503 });
  stubRaw(() => answer());
  const write = keyed({ value: "x" });
  const attempt = async (promise: Promise<unknown>) => (await failure(promise)).sent?.attempt;
  const retitle = () => setField("/api/inquiries/{target_id}/title", ID, write);
  expect(await attempt(retitle())).toBe(1);
  expect(await attempt(retitle())).toBe(2);
  // Another key is another request, though the URL is the same.
  expect(await attempt(setField("/api/inquiries/{target_id}/title", ID, keyed({ value: "x" })))).toBe(1);
  expect(await attempt(retitle())).toBe(3);
  answer = () => Response.json({ id: ID, change_id: null });
  await retitle();
  answer = () => Response.json({ detail: "down" }, { status: 503 });
  expect(await attempt(retitle())).toBe(1);
});

test("a request's path leaves out its query, where search text goes", async () => {
  const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
  stubRaw(() => Response.json({ detail: "statement timeout" }, { status: 400 }));
  const error = await failure(searchInquiries({ q: "my private words", kind: "Issue", limit: 5 }));
  expect(error.sent?.path).toBe("/api/web/search");
  expect(String(warn.mock.calls[0]![0])).not.toContain("private");
});

test("a success is logged at debug, off the console, with its id", async () => {
  const debug = vi.spyOn(console, "debug").mockImplementation(() => {});
  const sent = stubRaw(() => Response.json({}));
  await profile();
  expect(recentEvents().at(-1)).toMatchObject({
    level: "debug",
    event: "request",
    fields: { method: "GET", path: "/api/me/profile", status: 200, request_id: sent[0]!.headers.get("X-Request-ID") },
  });
  expect(debug).not.toHaveBeenCalled();
});
