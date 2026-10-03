// Test helpers for the API modules; only tests import this file.
import { vi } from "vitest";

/** One request as the server would receive it. */
export type Sent = {
  method: string;
  path: string;
  /** The query string, with its leading `?`, or `""`. */
  query: string;
  headers: { [name: string]: string };
  /** The parsed JSON body, or `undefined` when there was none. */
  body: unknown;
};

/**
 * Stub `fetch` for the current test; returns the requests it receives, in order.
 *
 * Every request gets `respond(request)`, by default an empty JSON object.
 * `headers` leaves out `X-Request-ID`, which is fresh on every request, so a
 * retry records the same as the first send; `client.test.ts` checks it.
 */
export function stubFetch(
  respond: (request: Request) => Response | Promise<Response> = () => Response.json({}),
): Sent[] {
  const sent: Sent[] = [];
  // A real `fetch` holds its request until it answers, and so must this one.
  // undici's Request follows the caller's signal only through a WeakRef, so an
  // answer that waits for the abort, with nothing else holding the request, could
  // be collected first; the abort then never came, and the test hung until its
  // timeout (search.test.ts, once in a full run; a forced GC does it every time).
  const answering = new Set<Request>();
  vi.stubGlobal("fetch", async (request: Request) => {
    answering.add(request);
    try {
      const url = new URL(request.url);
      const text = await request.clone().text();
      const { "x-request-id": _, ...headers } = Object.fromEntries(request.headers);
      sent.push({
        method: request.method,
        path: url.pathname,
        query: url.search,
        headers,
        body: text ? JSON.parse(text) : undefined,
      });
      return await respond(request);
    } finally {
      answering.delete(request);
    }
  });
  return sent;
}
